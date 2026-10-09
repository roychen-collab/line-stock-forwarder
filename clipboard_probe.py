"""以正常 LINE 介面全選、複製測試聊天文字；僅測試，不發送。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import math
import multiprocessing as mp
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parent


class FocusChangedError(RuntimeError):
    """目標仍存在，但鍵盤焦點已離開；呼叫端可選擇無輸入地暫停。"""
    error_code = "focus_changed"


class SourceUnavailableError(FocusChangedError):
    """來源暫時關閉或最小化；保留進度，不輸入、不自動登入或還原。"""
    error_code = "source_unavailable"


class ClipboardNotUpdatedError(RuntimeError):
    """正常複製未產生新剪貼簿內容；禁止讀取舊內容。"""


def capture(report_path: str, title: str, activate: bool = True, wait_ready: int = 0) -> None:
    import ctypes
    import psutil
    from pywinauto import Desktop, keyboard
    import win32clipboard as clipboard
    import win32gui
    import win32process

    report = {"status": "error", "method": "normal_ui_copy", "group": title,
              "text": None, "errors": []}
    try:
        pids = {p.info["pid"] for p in psutil.process_iter(["pid", "name"])
                if (p.info["name"] or "").lower() == "line.exe"}
        windows = [w for w in Desktop(backend="uia").windows()
                   if w.element_info.process_id in pids
                   and win32gui.GetWindowText(w.handle) == title]
        if not windows:
            raise SourceUnavailableError(f"LINE 聊天室「{title}」未開啟；請確認登入及聊天室視窗。")
        if len(windows) != 1:
            raise RuntimeError(f"找不到唯一的 LINE 聊天室「{title}」：找到 {len(windows)} 個。")
        window = windows[0]
        if window.is_minimized():
            raise SourceUnavailableError("聊天室已最小化；等待手動還原，不自動操作。")
        lists = window.descendants(control_type="List")
        edits = window.descendants(control_type="Edit")
        if len(lists) != 1 or len(edits) != 1:
            raise RuntimeError("聊天室版面與已測試版面不同：需要唯一的 List 與 Edit，已停止。")
        message_rect, input_rect = lists[0].rectangle(), edits[0].rectangle()
        if (message_rect.width() < 80 or message_rect.height() < 40
                or message_rect.bottom > input_rect.top):
            raise RuntimeError("無法確認訊息清單在輸入框上方，已停止。")

        def require_target() -> None:
            if not win32gui.IsWindow(window.handle):
                raise SourceUnavailableError("LINE 聊天室已關閉，停止本次操作。")
            if (win32gui.GetWindowText(window.handle) != title
                    or win32process.GetWindowThreadProcessId(window.handle)[1] not in pids):
                raise RuntimeError("LINE 聊天室或鍵盤焦點已變更，停止操作。"
                                   f" target_handle={window.handle},"
                                   f" foreground_handle={win32gui.GetForegroundWindow()},"
                                   f" target_exists={bool(win32gui.IsWindow(window.handle))}")
            if window.is_minimized():
                raise SourceUnavailableError("LINE 聊天室已最小化，停止本次操作。")
            if win32gui.GetForegroundWindow() != window.handle:
                raise FocusChangedError("LINE 聊天室已失去鍵盤焦點，停止本次操作。"
                                        f" target_handle={window.handle},"
                                        f" foreground_handle={win32gui.GetForegroundWindow()}")

        if wait_ready:
            deadline = time.monotonic() + wait_ready
            while True:
                try:
                    require_target()
                except SourceUnavailableError:
                    raise
                except FocusChangedError:
                    pass
                else:
                    if lists[0].has_keyboard_focus() and not edits[0].has_keyboard_focus():
                        break
                if time.monotonic() >= deadline:
                    report["verified_focus"] = {
                        "message_list": bool(lists[0].has_keyboard_focus()),
                        "input_box": bool(edits[0].has_keyboard_focus()),
                    }
                    raise TimeoutError("等待來源聊天室前景及訊息清單焦點逾時；沒有鍵盤操作或發送。")
                time.sleep(0.2)
        elif activate and win32gui.GetForegroundWindow() != window.handle:
            window.set_focus()
            time.sleep(0.1)
        require_target()
        # 自己的訊息在右側，原本的邊緣點擊可能落在氣泡文字上。
        # 直接用公開 UIA SetFocus 指向訊息清單，核對焦點後才全選／複製。
        if not lists[0].is_keyboard_focusable():
            raise RuntimeError("訊息清單不接受公開 UIA 鍵盤焦點，已停止。")
        if lists[0].has_keyboard_focus() and not edits[0].has_keyboard_focus():
            report["focus_method"] = "uia_message_list_already_focused"
        elif edits[0].has_keyboard_focus():
            # 本機實測：LINE 的 UIA SetFocus 不一定生效，正常 Tab 可從輸入框到訊息清單。
            # 只在輸入框焦點明確、來源仍在前景時嘗試一次；下方仍核對實際清單焦點。
            require_target()
            keyboard.send_keys("{TAB}", pause=0.1)
            report["focus_method"] = "normal_tab_from_verified_input"
        else:
            lists[0].set_focus()
            report["focus_method"] = "uia_message_list_set_focus"

        def require_message_focus():
            require_target()
            list_focused = bool(lists[0].has_keyboard_focus())
            input_focused = bool(edits[0].has_keyboard_focus())
            report["verified_focus"] = {"message_list": list_focused, "input_box": input_focused}
            if not list_focused or input_focused:
                raise RuntimeError("無法確認鍵盤焦點在訊息清單，禁止全選／複製。"
                                   f" message_list_focused={list_focused}, input_box_focused={input_focused}")

        require_message_focus()
        keyboard.send_keys("^a", pause=0.1)
        require_message_focus()
        sequence_before = ctypes.windll.user32.GetClipboardSequenceNumber()
        keyboard.send_keys("^c", pause=0.1)
        deadline = time.monotonic() + 3
        while ctypes.windll.user32.GetClipboardSequenceNumber() == sequence_before:
            require_target()
            if time.monotonic() >= deadline:
                raise ClipboardNotUpdatedError("複製後剪貼簿沒有更新，停止本次讀取；未讀取先前剪貼簿內容。")
            time.sleep(0.05)
        require_target()
        # 開啟期間鎖住剪貼簿；只接受來自目前 LINE 程序的標準 Unicode 文字。
        clipboard.OpenClipboard()
        try:
            owner = clipboard.GetClipboardOwner()
            if not owner or win32process.GetWindowThreadProcessId(owner)[1] != window.element_info.process_id:
                raise RuntimeError("剪貼簿來源不是目前的 LINE，已停止。")
            if not clipboard.IsClipboardFormatAvailable(clipboard.CF_UNICODETEXT):
                raise RuntimeError("LINE 複製結果沒有 Unicode 文字。")
            text = clipboard.GetClipboardData(clipboard.CF_UNICODETEXT)
            sequence_after = ctypes.windll.user32.GetClipboardSequenceNumber()
        finally:
            clipboard.CloseClipboard()
        if not text.strip():
            raise RuntimeError("LINE 複製結果為空白，已停止。")
        if not re.match(r"^\d{4}\.\d{2}\.\d{2} [^\r\n]+\r?\n\d{2}:\d{2} ", text):
            report["rejected_format"] = {"characters": len(text), "line_count": len(text.splitlines())}
            raise RuntimeError("複製內容不符合已測過的 LINE 聊天日期／時間格式，已停止；不當作聊天訊息。")
        report.update(status="completed", text=text, characters=len(text),
                      sequence_before=sequence_before, sequence_after=sequence_after)
    except Exception as exc:
        if isinstance(exc, FocusChangedError):
            report["error_code"] = exc.error_code
        elif isinstance(exc, ClipboardNotUpdatedError):
            report["error_code"] = "clipboard_not_updated"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def appended_text(previous: str, current: str) -> str:
    """只接受原內容後面新增；內容改寫或裁切時停止，避免猜測訊息邊界。"""
    previous = previous.replace("\r\n", "\n").replace("\r", "\n")
    current = current.replace("\r\n", "\n").replace("\r", "\n")
    if not current.startswith(previous):
        raise RuntimeError("聊天複製內容不再包含完整起始內容，可能有收回、改寫或載入範圍改變；已停止。")
    return current[len(previous):]


def run_capture(report_path: Path, title: str, activate: bool, wait_ready: int = 0) -> dict:
    worker = mp.Process(target=capture, args=(str(report_path), title, activate, wait_ready))
    worker.start()
    try:
        worker.join(20 + wait_ready)
        if worker.is_alive():
            raise TimeoutError(f"測試超過 {20 + wait_ready} 秒，已停止，不自動重試。")
        if worker.exitcode != 0 or not report_path.exists():
            raise RuntimeError(f"測試程序未產生有效報告；exitcode={worker.exitcode}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report["status"] != "completed":
            if report.get("error_code") == "clipboard_not_updated":
                raise ClipboardNotUpdatedError("; ".join(report["errors"]))
            if report.get("error_code") == "source_unavailable":
                raise SourceUnavailableError("; ".join(report["errors"]))
            if report.get("error_code") == "focus_changed":
                raise FocusChangedError("; ".join(report["errors"]))
            raise RuntimeError("; ".join(report["errors"]) or "複製失敗")
        return report
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", help="測試聊天室完整名稱；預設讀 config.json 的 test_group")
    parser.add_argument("--no-activate", action="store_true", help="不切換前景；檢查使用者已選好的聊天室焦點")
    parser.add_argument("--wait-foreground", type=int, help="等待來源聊天室前景及訊息清單焦點，最多 120 秒；不切換或設定焦點")
    parser.add_argument("--watch", action="store_true", help="只在 console 顯示新增聊天文字；不發送")
    parser.add_argument("--stop-after-new", action="store_true", help="搭配 --watch：讀到第一個新增區塊後停止，不發送")
    parser.add_argument("--max-checks", type=int, help="驗證用：檢查指定次數後退出")
    args = parser.parse_args()
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    report_path = logs / f"clipboard-{stamp}.json"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(logs / f"clipboard-{stamp}.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        title = args.group if args.group is not None else config.get("test_group", "")
        if not isinstance(title, str) or not title.strip() or title == "LINE":
            raise ValueError("請設定 test_group 或 --group，且不能指定 LINE 主視窗。")
        interval = config.get("polling_interval", 5)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 2:
            raise ValueError("polling_interval 必須是至少 2 秒的有限數字。")
        if args.max_checks is not None and args.max_checks < 1:
            raise ValueError("max-checks 必須至少為 1。")
        if args.stop_after_new and not args.watch:
            raise ValueError("--stop-after-new 必須搭配 --watch。")
        if args.wait_foreground is not None and not 1 <= args.wait_foreground <= 120:
            raise ValueError("--wait-foreground 必須是 1～120 秒。")
        logging.info("開始複製測試；群組=%s；正常複製會覆寫剪貼簿，不會發送訊息。", title)
        if args.wait_foreground is not None:
            logging.info("等待使用者選好前景聊天室訊息清單，最多 %s 秒；等待期間沒有鍵盤操作。", args.wait_foreground)
        if args.watch:
            logging.info("console 測試模式：啟動時略過舊內容；切換到其他視窗會停止。")
        previous = None
        checks = 0
        while True:
            # console 模式覆寫同一個 snapshot，避免每次輪詢存一份完整歷史。
            report = run_capture(report_path, title, activate=(checks == 0 and not args.no_activate
                                                             and args.wait_foreground is None),
                                 wait_ready=(args.wait_foreground or 0) if checks == 0 else 0)
            checks += 1
            if not args.watch:
                logging.info("成功取得 %s 個字元；以下是 LINE 正常複製出的內容：\n%s",
                             report["characters"], report["text"])
                break
            if previous is None:
                logging.info("已建立起始位置（%s 個字元），舊內容略過。", report["characters"])
            else:
                added = appended_text(previous, report["text"])
                if added:
                    logging.info("偵測到新增聊天文字（尚未分割或篩選訊息）：\n%s", added)
                    if args.stop_after_new:
                        logging.info("已取得第一個新增區塊，停止讀取；沒有發送。")
                        break
                else:
                    logging.info("未發現新增文字。")
            previous = report["text"]
            if args.max_checks and checks >= args.max_checks:
                logging.info("已完成 %s 次檢查，結束。", checks)
                break
            time.sleep(interval)
        logging.info("結果已保存：%s", report_path)
        return 0
    except KeyboardInterrupt:
        logging.info("使用者停止測試。")
        return 130
    except Exception as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
