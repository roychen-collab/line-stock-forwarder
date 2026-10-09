"""本機互動配對工具；不發送訊息，Token 不寫進專案檔案或 log。"""
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import threading
import time
import tkinter as tk
from tkinter import ttk

from telegram_client import TelegramError, call_api, load_token, store_token, validate_token

ROOT = Path(__file__).resolve().parent


def pairing_chats(updates: list, phrase: str, since: int) -> dict:
    matches = {}
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        author = message.get("from", {})
        if (message.get("text", "").strip() == phrase
                and message.get("date", 0) >= since
                and chat.get("type") == "private"
                and isinstance(chat.get("id"), int) and chat["id"] > 0
                and author.get("id") == chat["id"] and not author.get("is_bot", False)):
            matches[chat["id"]] = chat
    return matches


class SetupApp:
    def __init__(self, root: tk.Tk, saved_bot_id: int | None = None):
        self.root = root
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.logs = ROOT / "logs"
        self.logs.mkdir(exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.FileHandler(self.logs / f"telegram-setup-{stamp}.log", encoding="utf-8")])
        root.title("LINE 股票轉傳 — Telegram 接收端設定")
        root.geometry("600x470")
        root.minsize(580, 450)
        root.option_add("*Font", ("Microsoft JhengHei UI", 11))
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="設定公司手機的 Telegram 接收聊天室", font=("Microsoft JhengHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(frame, text="貼上你建立的 Bot Token。Token 不會出現在 log 或 config.json。\n驗證後，請在公司手機向你的 Bot 傳送畫面上的配對碼。", wraplength=550).pack(anchor="w", pady=12)
        self.token = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.token, show="●", width=65)
        self.entry.pack(fill="x", pady=4)
        self.button = ttk.Button(frame, text="驗證 Bot 並開始配對", command=self.submit)
        self.button.pack(anchor="w", pady=12)
        self.status = tk.StringVar(value="等待輸入 Token。此工具不會發送 LINE 或 Telegram 訊息。")
        ttk.Label(frame, textvariable=self.status, wraplength=550, justify="left").pack(anchor="w", pady=8)
        self.phrase = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.phrase, state="readonly", width=25,
                  font=("Consolas", 18)).pack(anchor="w", pady=8)
        ttk.Button(frame, text="關閉", command=self.close).pack(side="bottom", anchor="e")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.save_state("awaiting_token")
        root.after(100, self.drain)
        self.entry.focus_set()
        if saved_bot_id is not None:
            try:
                self.token.set(load_token(f"line-stock-forwarder/telegram/{saved_bot_id}"))
                root.after(200, self.submit)
            except TelegramError as exc:
                self.status.set(str(exc))

    def save_state(self, status: str, **fields):
        path = self.logs / "telegram-setup-state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"status": status,
                                        "time": datetime.now().astimezone().isoformat(),
                                        **fields}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def submit(self):
        try:
            token = validate_token(self.token.get())
        except TelegramError as exc:
            self.status.set(str(exc))
            return
        self.token.set("")
        self.entry.configure(state="disabled")
        self.button.configure(state="disabled")
        self.phrase.set("")
        self.status.set("正在向 Telegram 官方 API 驗證 Bot……")
        self.save_state("validating")
        threading.Thread(target=self.pair, args=(token,), daemon=True).start()

    def pair(self, token: str):
        try:
            # 設定檔無法讀取時，不進行 API 查詢或儲存認證。
            json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
            bot = call_api(token, "getMe")
            if not bot.get("is_bot") or not isinstance(bot.get("id"), int):
                raise TelegramError("API 回應不是有效的 Telegram Bot。")
            if call_api(token, "getWebhookInfo").get("url"):
                raise TelegramError("此 Bot 已設定 webhook。請使用本專案專用的新 Bot，程式不會移除既有設定。")
            phrase = f"pair {secrets.randbelow(1000000):06d}"
            since = int(time.time())
            self.events.put(("pairing", bot["username"], phrase))
            offset = None
            deadline = time.monotonic() + 300
            while not self.stop.is_set() and time.monotonic() < deadline:
                payload = {"timeout": 0, "allowed_updates": ["message"], "limit": 100}
                if offset is not None:
                    payload["offset"] = offset
                updates = call_api(token, "getUpdates", payload)
                matches = pairing_chats(updates, phrase, since)
                if len(matches) > 1:
                    raise TelegramError("多個帳號傳入相同配對碼，無法確認接收端，請重新配對。")
                if matches:
                    chat_id, chat = next(iter(matches.items()))
                    if self.stop.is_set():
                        return
                    config_path = ROOT / "config.json"
                    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                    credential = store_token(token, bot["id"])
                    config["telegram"] = {"bot_username": bot["username"], "bot_id": bot["id"],
                                          "chat_id": chat_id, "credential_target": credential}
                    temporary = config_path.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    os.replace(temporary, config_path)
                    self.events.put(("completed", bot["username"], chat_id,
                                     chat.get("first_name", "")))
                    return
                if updates:
                    offset = max(u["update_id"] for u in updates) + 1
                self.stop.wait(3)
            if not self.stop.is_set():
                raise TelegramError("五分鐘內沒有收到配對碼。請确认用公司手機向正確的 Bot 傳送，再重新開始。")
        except TelegramError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:
            # 不輸出原例外內容，避免非預期回應中含有 Token。
            self.events.put(("error", f"設定失敗（{type(exc).__name__}）；請檢查 config.json 與網路，或提供本機 log。"))

    def drain(self):
        while not self.events.empty():
            event = self.events.get()
            if event[0] == "pairing":
                _, username, phrase = event
                self.phrase.set(phrase)
                self.status.set(f"Bot 驗證成功：@{username}\n請在公司手機開啟這個 Bot，按 Start／開始，\n傳送下方完整配對碼。程式會等待最多五分鐘。")
                self.save_state("awaiting_pair", bot_username=username, pairing_code=phrase)
                logging.info("Bot 驗證成功，等待手機配對；bot_username=%s", username)
            elif event[0] == "completed":
                _, username, chat_id, name = event
                self.phrase.set("")
                self.status.set(f"配對完成！接收帳號：{name}，chat_id={chat_id}\nToken 已儲存到此 Windows 帳號的認證管理員。\n目前尚未發送訊息。請回到聊天告訴我「配對完成」。\n你可以關閉這個視窗。")
                self.save_state("completed", bot_username=username, chat_id=chat_id)
                logging.info("接收端配對完成；chat_id=%s；未發送訊息。", chat_id)
            elif event[0] == "error":
                self.phrase.set("")
                self.status.set(event[1])
                self.entry.configure(state="normal")
                self.button.configure(state="normal")
                self.save_state("error", error=event[1])
                logging.error("%s", event[1])
        if not self.stop.is_set():
            self.root.after(100, self.drain)

    def close(self):
        self.stop.set()
        self.root.destroy()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-bot-id", type=int, help="使用此專案已儲存的 Bot 憑證開始配對")
    args = parser.parse_args()
    root = tk.Tk()
    SetupApp(root, args.saved_bot_id)
    root.mainloop()
