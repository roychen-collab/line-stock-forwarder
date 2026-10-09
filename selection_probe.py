"""單次檢查 UIA 選取最新可見項目後，正常 Ctrl+C 是否提供該則訊息的身分；不發送。"""
import argparse
from datetime import datetime
import json
import multiprocessing as mp
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent


def probe(destination: str, title: str, item_from_bottom: int = 0):
    import psutil
    from pywinauto import Desktop, keyboard
    import win32clipboard as clipboard
    import win32gui
    import win32process

    report = {"status": "error", "group": title, "method": "single_item_selection_copy",
              "sent": False}
    try:
        pids = {p.pid for p in psutil.process_iter(["name"])
                if (p.info["name"] or "").lower() == "line.exe"}
        windows = [w for w in Desktop(backend="uia").windows()
                   if w.element_info.process_id in pids and win32gui.GetWindowText(w.handle) == title]
        if len(windows) != 1:
            raise RuntimeError("找不到唯一來源聊天室。")
        window = windows[0]
        if (window.is_minimized() or not Path(psutil.Process(window.element_info.process_id).exe()).as_posix().lower()
                .endswith('/line/bin/current/line.exe')):
            raise RuntimeError('來源不是已核對且未最小化的官方 LINE 聊天室。')

        def check():
            if (win32gui.GetForegroundWindow() != window.handle
                    or win32gui.GetWindowText(window.handle) != title):
                raise RuntimeError("來源聊天室失去焦點或已變更，已停止。")

        check()
        lists = window.descendants(control_type="List")
        edits = window.descendants(control_type="Edit")
        if len(lists) != 1 or len(edits) != 1:
            raise RuntimeError("訊息清單或輸入框不唯一。")
        if not lists[0].has_keyboard_focus():
            if edits[0].has_keyboard_focus():
                check()
                keyboard.send_keys('{TAB}', pause=0.1)
            else:
                lists[0].set_focus()
        def require_list_focus():
            check()
            report['verified_focus'] = {'message_list': bool(lists[0].has_keyboard_focus()),
                                        'input_box': bool(edits[0].has_keyboard_focus())}
            if not report['verified_focus']['message_list'] or report['verified_focus']['input_box']:
                raise RuntimeError('訊息清單焦點未確認；禁止單項複製。')
        require_list_focus()
        items = lists[0].descendants(control_type="ListItem")
        if not items:
            raise RuntimeError("沒有可見訊息列。")
        if not 0 <= item_from_bottom < len(items):
            raise RuntimeError('指定訊息列不在目前公開可讀清單中。')
        item = sorted(items, key=lambda i: i.rectangle().top, reverse=True)[item_from_bottom]
        report.update(item_from_bottom=item_from_bottom, visible_items=len(items))
        report["item_runtime_id"] = list(item.element_info.runtime_id)
        check()
        item.iface_selection_item.Select()
        check()
        report["selected_count"] = lists[0].iface_selection.GetCurrentSelection().Length
        if report["selected_count"] != 1:
            raise RuntimeError("無法確認只選取一個項目。")
        require_list_focus()
        before = clipboard.GetClipboardSequenceNumber()
        keyboard.send_keys("^c", pause=0.1)
        deadline = time.monotonic() + 3
        while clipboard.GetClipboardSequenceNumber() == before:
            check()
            if time.monotonic() >= deadline:
                raise RuntimeError("單項複製沒有更新剪貼簿，無法讀取訊息身分。")
            time.sleep(0.05)
        check()
        clipboard.OpenClipboard()
        try:
            owner = clipboard.GetClipboardOwner()
            if not owner or win32process.GetWindowThreadProcessId(owner)[1] != window.element_info.process_id:
                raise RuntimeError("剪貼簿不是來自指定 LINE 程序。")
            if not clipboard.IsClipboardFormatAvailable(clipboard.CF_UNICODETEXT):
                raise RuntimeError("單項複製沒有標準文字可核對訊息身分。")
            text = clipboard.GetClipboardData(clipboard.CF_UNICODETEXT)
            if clipboard.GetClipboardSequenceNumber() == before:
                raise RuntimeError('剪貼簿更新未確認。')
            sequence = clipboard.GetClipboardSequenceNumber()
        finally:
            clipboard.CloseClipboard()
        if not text.strip() or clipboard.GetClipboardSequenceNumber() != sequence:
            raise RuntimeError('複製文字空白或剪貼簿已變更。')
        report.update(status="completed", text=text,
                      sequence_before=before, sequence_after=clipboard.GetClipboardSequenceNumber())
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--group', help='本次唯讀探測的完整來源名稱；不修改 config')
    parser.add_argument('--item-from-bottom', type=int, default=0,
                        help='0=最新可見列；其他索引僅供已觀察版面的診斷')
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    title = args.group if args.group is not None else config.get("test_group")
    if not isinstance(title, str) or not title.strip() or title == "LINE":
        raise ValueError("請設定 test_group。")
    path = ROOT / "logs" / ("selection-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.parent.mkdir(exist_ok=True)
    if args.item_from_bottom < 0:
        parser.error('訊息列索引不得小於零。')
    worker = mp.Process(target=probe, args=(str(path), title, args.item_from_bottom))
    worker.start()
    try:
        worker.join(20)
        if worker.is_alive():
            raise TimeoutError("選取探測超過 20 秒，已停止。")
        if worker.exitcode != 0 or not path.exists():
            raise RuntimeError("探測程序未產生有效結果。")
        report = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("報告：", path)
        return 0 if report["status"] == "completed" else 1
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
