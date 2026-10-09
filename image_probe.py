"""驗證 LINE 正常複製的公開圖片格式，只存本機樣本，不發送。"""
import argparse
from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import warnings

from PIL import Image

ROOT = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 40 * 1024 * 1024


def decode_public_image(raw: bytes, format_name: str) -> Image.Image:
    if format_name not in {"CF_DIB", "CF_DIBV5", "PNG"}:
        raise ValueError("只接受公開的 DIB／PNG 圖片格式，不解析 LINE 私有格式。")
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("圖片資料空白或超過 40 MiB 探測上限。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as image:
            if image.format not in {"DIB", "PNG"}:
                raise ValueError("實際資料不是 DIB／PNG 圖片。")
            image.load()
            if image.width < 1 or image.height < 1:
                raise ValueError("圖片尺寸無效。")
            return image.copy()


def main() -> int:
    import win32clipboard
    import win32gui
    import win32process
    import psutil

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequence-before", type=int, required=True,
                        help="操作 LINE 圖片複製前的剪貼簿序號，用來拒絕先前剪貼簿資料")
    args = parser.parse_args()
    logs = ROOT / "logs"
    samples = ROOT / "work" / "image-probe"
    logs.mkdir(exist_ok=True)
    samples.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    report = {"status": "error", "time": datetime.now().astimezone().isoformat(),
              "method": "public_line_clipboard_image", "sent": False}
    try:
        sequence = win32clipboard.GetClipboardSequenceNumber()
        if sequence == args.sequence_before:
            raise ValueError("剪貼簿未更新，不讀取先前資料。")
        win32clipboard.OpenClipboard()
        try:
            owner = win32clipboard.GetClipboardOwner()
            if not owner or not win32gui.IsWindow(owner):
                raise ValueError("無法確認剪貼簿來源。")
            pid = win32process.GetWindowThreadProcessId(owner)[1]
            if psutil.Process(pid).name().lower() != "line.exe":
                raise ValueError("剪貼簿來源不是 LINE，未讀取內容。")
            candidates = [(17, "CF_DIBV5"), (8, "CF_DIB"),
                          (win32clipboard.RegisterClipboardFormat("PNG"), "PNG")]
            available = [(number, name) for number, name in candidates
                         if win32clipboard.IsClipboardFormatAvailable(number)]
            report["public_image_formats"] = [name for _, name in available]
            if not available:
                raise ValueError("LINE 複製結果沒有公開的 DIB／PNG 圖片；不解析私有資料。")
            number, name = available[0]
            raw = win32clipboard.GetClipboardData(number)
            if win32clipboard.GetClipboardSequenceNumber() != sequence:
                raise ValueError("剪貼簿在檢查期間變更，已停止。")
        finally:
            win32clipboard.CloseClipboard()
        image = decode_public_image(raw, name)
        destination = samples / f"line-image-{stamp}.png"
        image.save(destination, format="PNG")
        report.update(status="completed", clipboard_sequence=sequence, clipboard_owner_pid=pid,
                      format=name, width=image.width, height=image.height, mode=image.mode,
                      sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                      sample_path=str(destination),
                      limitation="本次僅確認 LINE 剪貼簿圖片，不代表已能自動辨識來源群、訊息順序或背景圖片。")
    except ValueError as exc:
        report["error"] = str(exc)
    except Exception as exc:
        report["error"] = f"圖片探測失敗（{type(exc).__name__}），已停止，不自動重試。"
    (logs / f"image-probe-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
