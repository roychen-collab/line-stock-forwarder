"""唯讀 LINE UIA 探測：不點擊、不輸入、不發送、不使用剪貼簿。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import logging
import multiprocessing as mp
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def probe(output: str, include_text: bool, handle: int | None,
          window_title: str | None) -> None:
    # UIA 放在獨立程序，若 LINE 卡住，父程序可停止本次探測。
    import psutil
    from pywinauto import Desktop
    import win32api
    import win32gui

    report = {"status": "error", "errors": [], "windows": [], "elements": []}
    try:
        report["foreground_handle"] = win32gui.GetForegroundWindow()
        report["monitors"] = [
            {"bounds": list(win32api.GetMonitorInfo(monitor)["Monitor"]),
             "work_area": list(win32api.GetMonitorInfo(monitor)["Work"])}
            for monitor, _, _ in win32api.EnumDisplayMonitors()
        ]
        pids = []
        for proc in psutil.process_iter(["pid", "name"]):
            if (proc.info["name"] or "").lower() == "line.exe":
                pids.append(proc.info["pid"])
        windows = [w for w in Desktop(backend="uia").windows()
                   if w.element_info.process_id in pids]
        for window in windows:
            info = window.element_info
            name = win32gui.GetWindowText(window.handle) or ""
            report["windows"].append({
                "handle": window.handle, "is_main": name == "LINE",
                "title": name if include_text or name == "LINE" else "[名稱已隱藏]",
                "visible": window.is_visible(), "minimized": window.is_minimized(),
                "native_bounds": list(win32gui.GetWindowRect(window.handle)),
            })
        candidates = [w for w in windows if
                      (w.handle == handle if handle else
                       win32gui.GetWindowText(w.handle) == (window_title or "LINE"))]
        if len(candidates) != 1:
            raise RuntimeError(f"指定 LINE 視窗必須唯一，找到 {len(candidates)} 個。")
        window = candidates[0]
        if window.is_minimized():
            raise RuntimeError("LINE 視窗已最小化。請手動還原，再重新探測。")

        stack = [(window, 0)]
        depth_limited = False
        while stack and len(report["elements"]) < 1000:
            control, depth = stack.pop()
            info = control.element_info
            name = info.name or ""
            item = {"depth": depth, "type": info.control_type,
                    "automation_id": info.automation_id,
                    "class_name": info.class_name, "name_length": len(name),
                    "name": name if include_text else ("[已隱藏]" if name else ""),
                    "patterns": {}, "errors": []}
            if info.control_type in {"Window", "List", "Edit"}:
                try:
                    rect = control.rectangle()
                    item["bounds"] = [rect.left, rect.top, rect.right, rect.bottom]
                    item["keyboard_focus"] = control.has_keyboard_focus()
                    item["keyboard_focusable"] = control.is_keyboard_focusable()
                except Exception as exc:
                    item["errors"].append(f"焦點／座標: {type(exc).__name__}: {exc}")
            # 不讀取輸入框值或密碼內容；空白草稿不是訊息。
            if info.control_type in {"Text", "Document", "ListItem", "Group", "Custom"}:
                for pattern in ("value", "text"):
                    try:
                        text = (control.iface_value.CurrentValue if pattern == "value"
                                else control.iface_text.DocumentRange.GetText(-1)) or ""
                        item["patterns"][pattern] = {
                            "length": len(text),
                            "text": text if include_text else ("[已隱藏]" if text else ""),
                        }
                    except Exception as exc:
                        if type(exc).__name__ != "NoPatternInterfaceError":
                            item["errors"].append(f"{pattern}: {type(exc).__name__}: {exc}")
            report["elements"].append(item)
            if depth < 20:
                try:
                    stack.extend((child, depth + 1) for child in reversed(control.children()))
                except Exception as exc:
                    report["errors"].append(f"列舉元素失敗: {type(exc).__name__}: {exc}")
            else:
                depth_limited = True
        report["truncated"] = bool(stack) or depth_limited
        counts = Counter(e["type"] for e in report["elements"])
        report["summary"] = {
            "element_count": len(report["elements"]), "control_types": dict(counts),
            "list_items_with_name": sum(e["type"] == "ListItem" and e["name_length"] > 0
                                        for e in report["elements"]),
            "elements_with_readable_pattern": sum(any(p["length"] > 0
                                                     for p in e["patterns"].values())
                                                  for e in report["elements"]),
            "verdict": "僅為元素探測；訊息內容、順序與新訊息讀取仍須測試驗證。",
        }
        report["status"] = "completed"
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-text", action="store_true",
                        help="在報告及 console 顯示文字；請先只開啟測試聊天室")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--window-handle", type=int, help="選擇報告列出的聊天室視窗 handle")
    target.add_argument("--window-title", help="指定獨立聊天室的完整視窗名稱（完全相符）")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("此工具僅支援 Windows。", file=sys.stderr)
        return 1
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    report_path = logs / f"uia-{stamp}.json"
    log_path = logs / f"diagnostic-{stamp}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    logging.info("開始唯讀探測；include_text=%s", args.include_text)
    worker = mp.Process(target=probe, args=(str(report_path), args.include_text,
                                          args.window_handle, args.window_title))
    worker.start()
    try:
        worker.join(30)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            logging.error("UIA 探測超過 30 秒，已停止；不自動重試。")
            return 2
        if not report_path.exists():
            logging.error("探測程序未產生報告；exitcode=%s", worker.exitcode)
            return 1
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for window in report["windows"]:
            logging.info("LINE 視窗: %s", json.dumps(window, ensure_ascii=False))
        for err in report["errors"]:
            logging.error("%s", err)
        for item in report["elements"]:
            logging.info("%s%s name=%r name_length=%s id=%r patterns=%s errors=%s",
                         "  " * item["depth"], item["type"], item["name"],
                         item["name_length"], item["automation_id"], item["patterns"], item["errors"])
        logging.info("摘要: %s", json.dumps(report.get("summary", {}), ensure_ascii=False))
        logging.info("報告: %s", report_path)
        return 0 if report["status"] == "completed" else 1
    except KeyboardInterrupt:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        logging.info("使用者停止探測。")
        return 130


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
