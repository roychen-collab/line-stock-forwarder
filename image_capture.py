"""受控單張圖片正常 UI 擷取；OCR 核對身分後才複製，不發送。"""
from datetime import datetime
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import time

from chat_text import parse_transcript
from clipboard_probe import run_capture
from image_probe import decode_public_image
from image_candidate import locate_image_candidate
from viewer_identity import read_fields, verify_fields
from windows_ocr import recognize_image

ROOT = Path(__file__).resolve().parent


def inspect_source(group: str, report_path: Path):
    """只讀取官方來源視窗的公開 UI 位置與畫面；不點擊、不改焦點。"""
    import ctypes
    from PIL import ImageGrab
    import psutil
    from pywinauto import Desktop
    import win32gui

    ctypes.windll.user32.SetProcessDPIAware()
    pids = {p.pid for p in psutil.process_iter(['name'])
            if (p.info['name'] or '').lower() == 'line.exe'}
    sources = [w for w in Desktop(backend='uia').windows()
               if w.element_info.process_id in pids and win32gui.GetWindowText(w.handle) == group]
    if len(sources) != 1:
        raise RuntimeError('找不到唯一來源聊天室，未擷取。')
    source = sources[0]
    if win32gui.IsIconic(source.handle) or win32gui.GetForegroundWindow() != source.handle:
        raise RuntimeError('來源不在前景，未擷取、不搶焦點。')
    bounds = tuple(win32gui.GetWindowRect(source.handle))
    elements = []
    for item in source.descendants():
        rect = item.rectangle()
        elements.append({'type': item.element_info.control_type,
                         'name': item.element_info.name,
                         'bounds': [rect.left, rect.top, rect.right, rect.bottom]})
    image = ImageGrab.grab(bbox=bounds, all_screens=True)
    if win32gui.GetForegroundWindow() != source.handle or win32gui.GetWindowRect(source.handle) != bounds:
        raise RuntimeError('來源焦點或位置變更，未保存畫面。')
    path = ROOT / 'work' / 'ocr-probe' / (report_path.stem + '-source.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format='PNG')
    report = {'status': 'completed', 'method': 'read_only_public_source_inspection',
              'group': group, 'bounds': bounds, 'image_path': str(path), 'elements': elements, 'sent': False}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def expected_image_message(transcript: str, senders: list[str], *, skip_recall_notices: bool = False):
    notices = []
    messages = parse_transcript(transcript, senders, skip_recall_notices=skip_recall_notices, notices=notices)
    if not messages or messages[-1].text != "圖片":
        raise ValueError("最後一則不是已實測的圖片提示，禁止操作。")
    expected = messages[-1]
    key = (expected.day, expected.clock, expected.sender)
    if (sum((m.day, m.clock, m.sender) == key for m in messages) != 1
            or any((n.day, n.clock, n.sender) == key for n in notices)):
        raise ValueError("同一發送者在同一分鐘有多則訊息或收回提示，無法唯一對應圖片；已停止。")
    last_line = transcript.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n').split('\n')[-1]
    if last_line != f'{expected.clock} {expected.sender} 圖片':
        raise ValueError("最新公開項目不是圖片；圖片後有收回提示或其他內容，禁止操作。")
    return expected, messages


def _worker(report_path: str, group: str, transcript: str, senders: list[str], skip_recall_notices: bool = False):
    import ctypes
    from PIL import ImageGrab
    import psutil
    from pywinauto import Desktop, mouse, keyboard
    from pywinauto.controls.hwndwrapper import InvalidWindowHandle
    import win32clipboard as clipboard
    import win32gui
    import win32process

    started = time.monotonic()
    report = {"status": "error", "group": group, "method": "ocr_verified_public_image_copy", "sent": False,
              "timings_seconds": {}}
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        expected, messages = expected_image_message(transcript, senders, skip_recall_notices=skip_recall_notices)
        if len({''.join(s.split()) for s in senders}) != len(senders):
            raise ValueError("發送者姓名移除 OCR 空白後不唯一，已停止。")
        pids = {p.pid for p in psutil.process_iter(["name"])
                if (p.info["name"] or "").lower() == "line.exe"}
        source = [w for w in Desktop(backend="uia").windows()
                  if w.element_info.process_id in pids and win32gui.GetWindowText(w.handle) == group]
        if len(source) != 1:
            raise RuntimeError("找不到唯一的來源聊天室。")
        source = source[0]
        source_pid = source.element_info.process_id
        bound_pids = {source.handle: source_pid}

        def require(handle):
            if (not win32gui.IsWindow(handle) or win32gui.IsIconic(handle)
                    or win32gui.GetForegroundWindow() != handle
                    or win32gui.GetWindowText(handle) != group
                    or win32process.GetWindowThreadProcessId(handle)[1] != bound_pids.get(handle)):
                report["focus_failure"] = {
                    "stage": report.get("stage"), "expected_handle": handle,
                    "foreground_handle": win32gui.GetForegroundWindow(),
                    "exists": bool(win32gui.IsWindow(handle)),
                    "minimized": bool(win32gui.IsIconic(handle)),
                }
                raise RuntimeError("來源／圖片檢視器焦點或視窗已變更，停止，不搶回焦點。")

        def viewers():
            found = []
            # 先依完整群組標題過濾，避免替無關、剛消失的暫時視窗建立 wrapper。
            try:
                matching = Desktop(backend="win32").windows(title=group)
            except InvalidWindowHandle:
                report["disappeared_viewer_enumerations"] = report.get("disappeared_viewer_enumerations", 0) + 1
                return []
            for w in matching:
                if win32gui.GetWindowText(w.handle) != group:
                    continue
                try:
                    executable = Path(psutil.Process(w.process_id()).exe())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if (executable.name.lower() == "linemediaplayer.exe"
                        and [p.name for p in list(executable.parents)[1:5]] == ["LineMediaPlayer", "plugin", "Data", "LINE"]):
                    found.append(w)
            return found

        report["stage"] = "source_ready"
        require(source.handle)
        if viewers():
            raise RuntimeError("已有此群組的圖片檢視器開啟，請先關閉，只保留來源聊天室。")
        lists = source.descendants(control_type="List")
        if len(lists) != 1:
            raise RuntimeError("訊息清單不唯一。")
        items = lists[0].descendants(control_type="ListItem")
        if not items:
            raise RuntimeError("沒有可見的訊息項目。")
        # 這只是候選位置。選錯候選時，後面的群組、發送者、日期與時間核對必須拒絕。
        rect = max(items, key=lambda item: item.rectangle().top).rectangle()
        viewport = lists[0].rectangle()
        top, bottom = max(rect.top, viewport.top), min(rect.bottom, viewport.bottom)
        if bottom - top < 40:
            raise RuntimeError("最新候選項目未充分顯示，請讓聊天室停在最底部。")
        left, right = max(rect.left, viewport.left), min(rect.right, viewport.right)
        require(source.handle)
        row = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        require(source.handle)
        source_dpi = ctypes.windll.user32.GetDpiForWindow(source.handle) or 96
        candidate_path = ROOT / 'work' / 'ocr-probe' / (Path(report_path).stem + '-candidate-row.png')
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        row.save(candidate_path, format='PNG')
        report['stage'] = 'locating_image_candidate'
        report['candidate'] = {'row_bounds': [left, top, right, bottom],
                               'source_dpi': source_dpi, 'sample_path': str(candidate_path)}
        candidate = locate_image_candidate(row, source_dpi)
        x1, y1, x2, y2 = candidate
        point = (left + (x1 + x2) // 2, top + (y1 + y2) // 2)
        report['candidate'].update(image_bounds_in_row=candidate, point=point)
        if lists[0].rectangle() != viewport or max(items, key=lambda item: item.rectangle().top).rectangle() != rect:
            raise RuntimeError('圖片位置在觀察後改變，禁止點擊。')
        require(source.handle)
        report["stage"] = "opening_viewer"
        opening_started = time.monotonic()
        mouse.double_click(coords=point)
        deadline = time.monotonic() + 5
        while True:
            candidates = viewers()
            if len(candidates) == 1:
                viewer = candidates[0]
                bound_pids[viewer.handle] = viewer.process_id()
                break
            if len(candidates) > 1 or time.monotonic() >= deadline:
                raise RuntimeError("正常開啟沒有產生唯一圖片檢視器，已停止。")
            time.sleep(0.1)
        report["stage"] = "viewer_opened"
        require(viewer.handle)
        report["timings_seconds"]["open_viewer"] = round(time.monotonic() - opening_started, 3)
        ocr_started = time.monotonic()
        left, top, right, bottom = win32gui.GetWindowRect(viewer.handle)
        dpi = ctypes.windll.user32.GetDpiForWindow(viewer.handle) or 96
        scale = dpi / 96
        if right - left < 450 * scale or bottom - top < 450 * scale:
            raise RuntimeError("圖片檢視器太小，不符合已校準的版面。")
        point = ((left + right) // 2, (top + bottom) // 2)
        require(viewer.handle)
        mouse.click(button="right", coords=point)
        time.sleep(0.2)
        for check_number in range(2):
            require(viewer.handle)
            header = ImageGrab.grab(bbox=(left, top, right, top + round(70 * scale)), all_screens=True)
            require(viewer.handle)
            header_path = ROOT / "work" / "ocr-probe" / (Path(report_path).stem + f"-header-{check_number + 1}.png")
            header_path.parent.mkdir(parents=True, exist_ok=True)
            header.save(header_path, format="PNG")
            report[f"header_sample_{check_number + 1}"] = str(header_path)
            report["dpi"] = dpi
            fields = read_fields(header, dpi)
            report[f"header_check_{check_number + 1}"] = fields
            report["identity"] = verify_fields(fields, group, expected, messages)
            require(viewer.handle)
        report["timings_seconds"]["verify_headers"] = round(time.monotonic() - ocr_started, 3)
        copying_started = time.monotonic()
        menu_right = min(right, point[0] + round(160 * scale))
        menu_bottom = min(bottom, point[1] + round(210 * scale))
        menu = ImageGrab.grab(bbox=(point[0], point[1], menu_right, menu_bottom), all_screens=True)
        require(viewer.handle)
        lines = recognize_image(menu)["lines"]
        report["menu_text"] = [line["text"] for line in lines]
        copies = [line for line in lines if ''.join(line["text"].split()) == "複製"]
        if len(copies) != 1 or not any(''.join(line["text"].split()) == "分享" for line in lines):
            raise RuntimeError("OCR 未找到唯一「複製」及已知「分享」選單，未點擊。")
        words = copies[0]["words"]
        x1, y1 = min(w["x"] for w in words), min(w["y"] for w in words)
        x2 = max(w["x"] + w["width"] for w in words)
        y2 = max(w["y"] + w["height"] for w in words)
        if not (0 <= y1 < y2 <= 50 * scale and 0 <= x1 < x2 <= 150 * scale):
            raise RuntimeError("複製選單位置與已校準版面不符，未點擊。")
        report["stage"] = "copy_menu_verified"
        before = clipboard.GetClipboardSequenceNumber()
        require(viewer.handle)
        mouse.click(coords=(round(point[0] + (x1 + x2) / 2), round(point[1] + (y1 + y2) / 2)))
        deadline = time.monotonic() + 3
        while clipboard.GetClipboardSequenceNumber() == before:
            require(viewer.handle)
            if time.monotonic() >= deadline:
                raise RuntimeError("圖片複製未更新剪貼簿，禁止讀取舊圖片。")
            time.sleep(0.05)
        report["stage"] = "image_copied"
        require(viewer.handle)
        sequence = clipboard.GetClipboardSequenceNumber()
        clipboard.OpenClipboard()
        try:
            owner = clipboard.GetClipboardOwner()
            if not owner or win32process.GetWindowThreadProcessId(owner)[1] != source_pid:
                raise RuntimeError("圖片剪貼簿來源不是指定 LINE 程序。")
            formats = [(17, "CF_DIBV5"), (8, "CF_DIB"), (clipboard.RegisterClipboardFormat("PNG"), "PNG")]
            available = [(number, name) for number, name in formats if clipboard.IsClipboardFormatAvailable(number)]
            if not available:
                raise RuntimeError("複製沒有公開圖片格式，禁止解析 LINE 私有格式。")
            number, format_name = available[0]
            raw = clipboard.GetClipboardData(number)
            if clipboard.GetClipboardSequenceNumber() != sequence:
                raise RuntimeError("圖片剪貼簿在檢查期間已變更。")
        finally:
            clipboard.CloseClipboard()
        image = decode_public_image(raw, format_name)
        sample = ROOT / "work" / "line-images" / (Path(report_path).stem + ".png")
        sample.parent.mkdir(parents=True, exist_ok=True)
        image.save(sample, format="PNG")
        report["timings_seconds"]["menu_and_copy"] = round(time.monotonic() - copying_started, 3)
        report["stage"] = "closing_viewer"
        require(viewer.handle)
        keyboard.send_keys("{ESC}")
        deadline = time.monotonic() + 2
        while win32gui.IsWindow(viewer.handle):
            if time.monotonic() >= deadline:
                raise RuntimeError("圖片擷取後未正常返回來源聊天室；未發送。")
            time.sleep(0.05)
        report["stage"] = "returning_to_source"
        # 官方獨立檢視器關閉後可能把焦點交給 Codex，而非開圖的聊天室。
        # 只有本程式已核對、複製、主動關閉的流程，才正常返回原來源一次。
        # 擷取過程的外部焦點變更仍由前面的 require() 立即停止。
        if (not win32gui.IsWindow(source.handle) or win32gui.IsIconic(source.handle)
                or win32gui.GetWindowText(source.handle) != group
                or win32process.GetWindowThreadProcessId(source.handle)[1] != source_pid):
            raise RuntimeError("圖片擷取後來源聊天室已變更；未返回、未發送。")
        source.set_focus()
        time.sleep(0.1)
        require(source.handle)
        report.update(status="completed", sample_path=str(sample), width=image.width, height=image.height,
                      bytes=sample.stat().st_size, sha256=hashlib.sha256(sample.read_bytes()).hexdigest(),
                      clipboard_sequence=sequence, clipboard_owner_pid=source_pid, format=format_name)
    except (ValueError, RuntimeError) as exc:
        report["error"] = str(exc)
    except Exception as exc:
        report["error"] = f"自動圖片擷取失敗（{type(exc).__name__}），已停止，不自動重試。"
    report["timings_seconds"]["worker_total"] = round(time.monotonic() - started, 3)
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run_image_capture(report_path: Path, group: str, transcript: str, senders: list[str], *,
                      skip_recall_notices: bool = False) -> dict:
    started = time.monotonic()
    report = {"status": "error", "group": group, "method": "ocr_verified_public_image_copy", "sent": False,
              "stage": "starting_worker"}
    worker = mp.Process(target=_worker, args=(str(report_path), group, transcript, senders, skip_recall_notices))
    worker.start()
    try:
        worker.join(30)
        if worker.is_alive():
            raise TimeoutError("圖片擷取超過三十秒，已停止，不重試。")
        if worker.exitcode != 0 or not report_path.exists():
            raise RuntimeError(f"圖片擷取沒有有效報告；worker exitcode={worker.exitcode}。")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") != "completed":
            raise RuntimeError(report.get("error", "圖片擷取失敗。"))
        report["stage"] = "verify_chat_snapshot"
        verification_started = time.monotonic()
        after = run_capture(report_path.with_name(report_path.stem + "-after.json"), group, activate=False)["text"]
        if after != transcript:
            report.update(status="error", error="圖片擷取期間聊天內容變更；未發送。")
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError("圖片擷取期間聊天內容變更；未發送，禁止猜測訊息對應。")
        report.setdefault("timings_seconds", {}).update(
            verify_chat_snapshot=round(time.monotonic() - verification_started, 3),
            capture_total=round(time.monotonic() - started, 3))
        report["stage"] = "completed"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    except (ValueError, RuntimeError, TimeoutError) as exc:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        report.update(status="error", error=str(exc))
        report.setdefault("timings_seconds", {})["capture_total"] = round(time.monotonic() - started, 3)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


def main():
    import sys
    import argparse
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect-source', action='store_true', help='只讀取來源 UI 位置及畫面，不點擊、不複製、不發送')
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    group = config["test_group"]
    path = ROOT / "logs" / ("image-capture-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.parent.mkdir(exist_ok=True)
    try:
        if args.inspect_source:
            report = inspect_source(group, path)
            print(json.dumps({'report': str(path), 'image_path': report['image_path'], 'sent': False}, ensure_ascii=False))
            return 0
        transcript = run_capture(path.with_name(path.stem + "-before.json"), group, activate=True)["text"]
        report = run_image_capture(path, group, transcript, config["test_senders"],
                                   skip_recall_notices=config.get("skip_recall_notices", False))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(f"圖片擷取已停止：{exc}")
        return 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
