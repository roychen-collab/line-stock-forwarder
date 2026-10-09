"""單次測試公開 UIA 選取與 pywinauto 背景複製；不發送、不使用私有格式。"""
import argparse
from datetime import datetime
import json
import multiprocessing as mp
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parent


def probe(path: str, title: str, keys_only: bool = False):
    import ctypes
    import psutil
    import win32clipboard
    import win32gui
    import win32process
    from pywinauto import Desktop

    report = {"status": "error", "method": "background_keys_copy" if keys_only else "uia_selection_background_copy", "group": title,
              "text": None, "errors": [], "foreground_unchanged": None}
    foreground = win32gui.GetForegroundWindow()
    report["foreground_before"] = foreground
    try:
        pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "line.exe"}
        windows = [w for w in Desktop(backend="uia").windows()
                   if w.element_info.process_id in pids and win32gui.GetWindowText(w.handle) == title]
        if len(windows) != 1:
            raise RuntimeError("找不到唯一測試聊天室，已停止。")
        window = windows[0]
        if window.handle == foreground or window.is_minimized():
            raise RuntimeError("此測試要求 LINE 在背景但未最小化；請先切到其他程式。")
        lists = window.descendants(control_type="List")
        edits = window.descendants(control_type="Edit")
        if len(lists) != 1 or len(edits) != 1:
            raise RuntimeError("LINE 版面與已測試版面不同，已停止。")
        items = lists[0].descendants(control_type="ListItem")
        if not items:
            raise RuntimeError("訊息清單沒有可選取的公開 UIA 元素。")
        native = Desktop(backend="win32").window(handle=window.handle).wrapper_object()
        report["native_child_count"] = len(native.descendants())
        report["list_native_handle"] = lists[0].handle
        report["visible_item_count"] = len(items)

        def check_focus():
            if win32gui.GetForegroundWindow() != foreground:
                raise RuntimeError("此背景操作改變了前景視窗，不符合需求，已停止，不繼續按鍵。")
            if not win32gui.IsWindow(window.handle) or win32gui.GetWindowText(window.handle) != title:
                raise RuntimeError("測試聊天室已變更，已停止。")

        if not keys_only:
            # 使用公開 SelectionItemPattern；不以滑鼠點擊或 SetFocus 啟用 LINE。
            check_focus()
            items[0].iface_selection_item.Select()
            check_focus()
            for item in items[1:]:
                item.iface_selection_item.AddToSelection()
                check_focus()
            report["selected_count"] = lists[0].iface_selection.GetCurrentSelection().Length
        else:
            check_focus()
            native.send_keystrokes("^a")
            check_focus()
        sequence = ctypes.windll.user32.GetClipboardSequenceNumber()
        check_focus()
        native.send_keystrokes("^c")  # 只測 Ctrl+C；沒有全域 SendInput 或文字輸入。
        check_focus()
        deadline = time.monotonic() + 3
        while ctypes.windll.user32.GetClipboardSequenceNumber() == sequence:
            check_focus()
            if time.monotonic() >= deadline:
                raise RuntimeError("背景複製後剪貼簿沒有更新；LINE 未接受這種背景複製。")
            time.sleep(0.05)
        win32clipboard.OpenClipboard()
        try:
            owner = win32clipboard.GetClipboardOwner()
            if not owner or win32process.GetWindowThreadProcessId(owner)[1] != window.element_info.process_id:
                raise RuntimeError("更新的剪貼簿不是來自指定 LINE，未讀取內容。")
            # 僅列舉格式名稱，不讀取 LINE 自訂資料格式。
            formats = []
            number = 0
            standard = {1: "CF_TEXT", 2: "CF_BITMAP", 8: "CF_DIB", 13: "CF_UNICODETEXT", 17: "CF_DIBV5"}
            while True:
                number = win32clipboard.EnumClipboardFormats(number)
                if not number:
                    break
                formats.append(standard.get(number) or (win32clipboard.GetClipboardFormatName(number) if number >= 0xC000 else str(number)))
            report["clipboard_formats"] = formats
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                raise RuntimeError("背景複製結果沒有標準 Unicode 聊天文字。")
            text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        check_focus()
        if not re.match(r"^(?:\d{4}\.\d{2}\.\d{2} [^\r\n]+\r?\n)?\d{2}:\d{2} ", text):
            raise RuntimeError("複製結果不是已驗證的聊天匯出格式；未保存內容，不當作訊息。")
        report.update(status="completed", text=text, characters=len(text))
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        report["foreground_after"] = win32gui.GetForegroundWindow()
        report["foreground_unchanged"] = report["foreground_after"] == foreground
        Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", help="預設使用 config.json 的 test_group")
    parser.add_argument("--keys-only", action="store_true", help="只測公開的背景 Ctrl+A／Ctrl+C，不使用 UIA 選取")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    title = args.group or config.get("test_group")
    if not isinstance(title, str) or not title.strip() or title == "LINE":
        raise ValueError("請先設定唯一的 test_group。")
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / ("background-" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    worker = mp.Process(target=probe, args=(str(path), title, args.keys_only))
    worker.start()
    try:
        worker.join(20)
        if worker.is_alive():
            raise TimeoutError("背景探測超過 20 秒，已停止；不重試。")
        if worker.exitcode != 0 or not path.exists():
            raise RuntimeError("背景探測未產生有效報告。")
        report = json.loads(path.read_text(encoding="utf-8"))
        # console 不輸出完整聊天歷史。
        print(json.dumps({k: v for k, v in report.items() if k != "text"}, ensure_ascii=False, indent=2))
        print("報告：", path)
        return 0 if report["status"] == "completed" else 1
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
