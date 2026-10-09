"""只截取前景 LINE 圖片檢視器標頭，以 Windows OCR／RapidOCR 在本機核對；不點擊、不複製、不發送。"""
import argparse
from datetime import datetime
import json
import multiprocessing as mp
from pathlib import Path
import sys

from windows_ocr import recognize_image
from viewer_identity import read_fields, verify_fields

ROOT = Path(__file__).resolve().parent


def worker_outcome(destination: Path, group: str, exit_code: int | None,
                   failure: str | None = None):
    """子程序原生崩潰時也保留最後階段，不能把已有截圖當成成功。"""
    report = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {
        "group": group, "method": "foreground_viewer_header_local_ocr",
        "sent": False, "network_used": False}
    if failure or exit_code != 0 or report.get("status") not in {"completed", "error"}:
        report.update(status="error", worker_exit_code=exit_code,
                      error=failure or f"OCR 子程序未完成有效報告（退出碼 {exit_code}）；未發送。")
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def probe(destination: str, group: str, handle: int, expected_snapshot: str | None = None):
    import ctypes
    from PIL import ImageGrab
    import psutil
    import win32gui
    import win32process

    report = {"status": "error", "method": "foreground_viewer_header_local_ocr",
              "group": group, "sent": False, "network_used": False}
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        if not win32gui.IsWindow(handle) or win32gui.GetWindowText(handle) != group:
            raise RuntimeError("圖片檢視器視窗或群組名稱不相符。")
        pid = win32process.GetWindowThreadProcessId(handle)[1]
        process = psutil.Process(pid)
        executable = Path(process.exe())
        if (process.name().lower() != "linemediaplayer.exe"
                or executable.parent.parent.name != "LineMediaPlayer"
                or executable.parent.parent.parent.name != "plugin"
                or executable.parent.parent.parent.parent.name != "Data"
                or executable.parent.parent.parent.parent.parent.name != "LINE"):
            raise RuntimeError("指定視窗不是已觀察到的官方 LINE 圖片檢視器。")

        def check():
            if (win32gui.GetForegroundWindow() != handle or win32gui.IsIconic(handle)
                    or win32gui.GetWindowText(handle) != group
                    or win32process.GetWindowThreadProcessId(handle)[1] != pid):
                raise RuntimeError("LINE 圖片檢視器不在前景或已變更，未繼續辨識。")

        check()
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        dpi = ctypes.windll.user32.GetDpiForWindow(handle) or 96
        header_height = round(70 * dpi / 96)
        if right - left < 300 or bottom - top < header_height * 2:
            raise RuntimeError("圖片檢視器尺寸與已觀察的版面不符。")
        image = ImageGrab.grab(bbox=(left, top, right, top + header_height), all_screens=True)
        check()
        sample = ROOT / "work" / "ocr-probe" / (Path(destination).stem + "-header.png")
        sample.parent.mkdir(parents=True, exist_ok=True)
        image.save(sample, format="PNG")
        report.update(status="running", stage="identity_fields_ocr", handle=handle, pid=pid,
                      dpi=dpi, header_size=list(image.size), sample_path=str(sample))
        Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # 和實際圖片擷取保持同一初始化順序；完整標頭 Windows OCR 僅供診斷。
        fields = read_fields(image, dpi)
        report.update(fields=fields, stage="windows_header_ocr")
        Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        result = recognize_image(image)
        check()
        report.update(status="completed", handle=handle, pid=pid, dpi=dpi,
                      stage="completed",
                      header_size=list(image.size), sample_path=str(sample), ocr=result, fields=fields,
                      limitation="本次只辨識標頭；不代表已可靠核對訊息位置、同分鐘圖片順序或自動轉傳。")
        if expected_snapshot:
            from image_capture import expected_image_message
            snapshot = json.loads(Path(expected_snapshot).read_text(encoding="utf-8"))
            config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
            if snapshot.get("status") != "completed" or snapshot.get("group") != group:
                raise ValueError("參考聊天快照不是成功的同群組快照。")
            expected, messages = expected_image_message(snapshot["text"], config["test_senders"],
                                                        skip_recall_notices=config.get("skip_recall_notices", False))
            report["verified_identity"] = verify_fields(fields, group, expected, messages)
    except (ValueError, RuntimeError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    except Exception as exc:
        report["status"] = "error"
        report["error"] = f"OCR 探測失敗（{type(exc).__name__}），已停止，不自動重試。"
    Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-handle", type=int, required=True,
                        help="當次已確認的 LINE 圖片檢視器 handle，不是聊天室 handle")
    parser.add_argument("--group", help="本次唯讀探測的完整群組名稱；不修改 config")
    parser.add_argument("--expected-snapshot", type=Path, help="成功的同群組聊天複製 JSON，用來核對最後一則身分")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    group = args.group if args.group is not None else config.get("test_group")
    if not isinstance(group, str) or not group.strip() or group == "LINE":
        raise ValueError("請設定 test_group。")
    if args.expected_snapshot and group != config.get("test_group"):
        parser.error("最後一則圖片核對仍只適用設定的測試群；正式群可先唯讀辨識標頭。")
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    destination = logs / ("ocr-probe-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    worker = mp.Process(target=probe, args=(str(destination), group, args.window_handle,
                                         str(args.expected_snapshot) if args.expected_snapshot else None))
    worker.start()
    try:
        worker.join(30)
        failure = None
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            failure = "OCR 超過三十秒，已停止；未發送。"
        report = worker_outcome(destination, group, worker.exitcode, failure)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("報告：", destination)
        return 0 if report["status"] == "completed" else 1
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
