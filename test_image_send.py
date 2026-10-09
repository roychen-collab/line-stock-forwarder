"""只將成功的 LINE 圖片探測樣本送到已配對 Telegram 一次；不是自動圖片監控。"""
import argparse
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sys

from image_probe import decode_public_image
from telegram_client import TelegramError, call_api, load_token, send_image

ROOT = Path(__file__).resolve().parent
CAPTION = "LINE 圖片接收測試：請放大確認圖中文字。尚未啟動自動圖片轉傳。"


def verified_sample(report_path: Path) -> tuple[dict, bytes]:
    report_path = report_path.resolve()
    if report_path.parent != (ROOT / "logs").resolve() or not report_path.name.startswith("image-probe-"):
        raise ValueError("只接受本專案 logs 中的 image-probe 報告。")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("status") != "completed"
            or report.get("method") != "public_line_clipboard_image"
            or report.get("format") not in {"PNG", "CF_DIB", "CF_DIBV5"}):
        raise ValueError("圖片探測沒有成功，禁止發送。")
    sample = Path(report["sample_path"]).resolve()
    if sample.parent != (ROOT / "work" / "image-probe").resolve() or sample.suffix.lower() != ".png":
        raise ValueError("圖片樣本必須位於本專案 work/image-probe。")
    if sample.stat().st_size > 40 * 1024 * 1024:
        raise ValueError("圖片樣本超過 40 MiB 上限。")
    png = sample.read_bytes()
    if hashlib.sha256(png).hexdigest() != report.get("sha256"):
        raise ValueError("圖片樣本在探測後已改變，禁止發送。")
    image = decode_public_image(png, "PNG")
    if image.size != (report.get("width"), report.get("height")):
        raise ValueError("圖片尺寸與探測報告不符，禁止發送。")
    return report, png


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="成功的 image-probe JSON 報告")
    args = parser.parse_args()
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    handlers = [logging.FileHandler(logs / f"image-send-test-{stamp}.log", encoding="utf-8"),
                logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)
    attempted = False
    try:
        report, png = verified_sample(args.report)
        target = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))["telegram"]
        chat_id, bot_id = target["chat_id"], target["bot_id"]
        if type(chat_id) is not int or chat_id <= 0 or type(bot_id) is not int:
            raise ValueError("接收端須為已配對的私人聊天室。")
        if target["credential_target"] != f"line-stock-forwarder/telegram/{bot_id}":
            raise ValueError("Bot 認證與設定不相符。")
        pairing = json.loads((logs / "telegram-setup-state.json").read_text(encoding="utf-8"))
        if (pairing.get("status") != "completed" or pairing.get("chat_id") != chat_id
                or pairing.get("bot_username") != target["bot_username"]):
            raise ValueError("目的地與成功配對紀錄不相符。")
        # 此為一次性樣本測試。正式監控不能以圖像 hash 判斷訊息重複。
        key = f"{args.report.resolve().name}/{chat_id}/{report['sha256']}"
        marker = logs / f"image-send-test-{hashlib.sha256(key.encode('utf-8')).hexdigest()}-state.json"
        if marker.exists():
            raise ValueError("此樣本已有發送嘗試紀錄；請先查看手機，不直接重送。")
        token = load_token(target["credential_target"])
        bot = call_api(token, "getMe")
        if not bot.get("is_bot") or bot.get("id") != bot_id or bot.get("username") != target["bot_username"]:
            raise ValueError("Bot 身分與配對設定不相符。")
        state = {"status": "attempting", "time": datetime.now().astimezone().isoformat(),
                 "chat_id": chat_id, "sha256": report["sha256"], "bytes": len(png),
                 "width": report["width"], "height": report["height"],
                 "report": args.report.resolve().name}
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        attempted = True
        logging.info("單次圖片接收測試；%s × %s；%s bytes；不自動重試。",
                     report["width"], report["height"], len(png))
        result = send_image(token, chat_id, png, CAPTION)
        state.update(status="sent", message_id=result["message_id"])
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        logging.info("成功；Telegram message_id=%s；目的地與檔案大小核對一致。", result["message_id"])
        return 0
    except (ValueError, TelegramError) as exc:
        logging.error("%s；已停止。", exc)
    except Exception as exc:
        logging.error("圖片測試失敗（%s）；已停止。", type(exc).__name__)
    finally:
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()
    if attempted:
        print("結果可能已送出；不自動重試，請先查看 Telegram 與 log。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
