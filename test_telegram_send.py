"""一次性 Telegram 接收測試；不讀取 LINE，也不轉傳聊天歷史。"""
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys

from telegram_client import TelegramError, call_api, load_token, send_text

ROOT = Path(__file__).resolve().parent
TEST_TEXT = "Telegram 接收測試成功\n中文、換行與符號：台積電 2330，123.45，ABC % & 😀\n這是一則連線測試，尚未啟動 LINE 自動轉傳。"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(logs / f"telegram-send-test-{stamp}.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    marker = logs / "telegram-send-test-state.json"
    attempted = False
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        target = config["telegram"]
        chat_id, bot_id = target["chat_id"], target["bot_id"]
        if type(chat_id) is not int or chat_id <= 0 or type(bot_id) is not int:
            raise TelegramError("接收端設定不完整，請重新配對。")
        if target["credential_target"] != f"line-stock-forwarder/telegram/{bot_id}":
            raise TelegramError("Bot 與本專案的認證名稱不一致，已停止。")
        pairing = json.loads((logs / "telegram-setup-state.json").read_text(encoding="utf-8"))
        if (pairing.get("status") != "completed" or pairing.get("chat_id") != chat_id
                or pairing.get("bot_username") != target["bot_username"]):
            raise TelegramError("找不到與設定相符的完成配對紀錄，已停止。")
        if marker.exists():
            raise TelegramError("本次接收測試已執行或結果待確認；為避免重複，已停止。請查看手機及測試 log。")
        token = load_token(target["credential_target"])
        bot = call_api(token, "getMe")
        if not bot.get("is_bot") or bot.get("id") != bot_id or bot.get("username") != target["bot_username"]:
            raise TelegramError("實際 Bot 與配對設定不一致，已停止。")
        state = {"status": "attempting", "time": datetime.now().astimezone().isoformat(),
                 "bot_username": target["bot_username"], "chat_id": chat_id}
        # 在網路發送前排他建立紀錄；中斷後也不會重跑同一次測試。
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        attempted = True
        logging.info("開始單次發送接收測試；chat_id=%s；不自動重試。", chat_id)
        sent = send_text(token, chat_id, TEST_TEXT)
        state.update(status="sent", message_id=sent["message_id"], text_verified=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        logging.info("Telegram 確認發送成功；message_id=%s；文字及目的地核對一致。", sent["message_id"])
        return 0
    except TelegramError as exc:
        logging.error("%s", exc)
    except Exception as exc:
        logging.error("接收測試停止（%s）；請檢查設定及網路。", type(exc).__name__)
    if attempted:
        logging.error("已開始一次發送嘗試，結果可能已送出；不重試，請先查看公司手機。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
