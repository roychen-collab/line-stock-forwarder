"""Telegram 官方 API 與本專案自己的 Windows 認證；不讀取 LINE 憑證。"""
import json
import re
import secrets
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    pass


def validate_token(token: str) -> str:
    token = token.strip()
    if len(token) > 512 or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise TelegramError("Token 格式不正確，請只貼上 BotFather 提供的 Token。")
    return token


def call_api(token: str, method: str, payload: dict | None = None):
    # 配對工具仍只能查詢；發送必須使用獨立的 send_text。
    if method not in {"getMe", "getWebhookInfo", "getUpdates"}:
        raise TelegramError("查詢介面不允許呼叫這個 API 方法。")
    return _request_api(token, method, payload)


def _request_api(token: str, method: str, payload: dict | None = None):
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    return _request_bytes(token, method, data, "application/json", timeout=15)


def _request_bytes(token: str, method: str, data: bytes, content_type: str, *, timeout: int):
    token = validate_token(token)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data,
        headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # 不格式化 HTTPError；它可能包含帶 Token 的 URL。
        hint = {401: "Token 無效或已撤銷", 409: "Bot 正被其他程式接收訊息，請先停止另一個程式"}.get(exc.code, "API 請求失敗")
        raise TelegramError(f"Telegram HTTP {exc.code}：{hint}。") from None
    except Exception as exc:
        raise TelegramError(f"Telegram 連線或回應處理失敗（{type(exc).__name__}）；請檢查網路。") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("error_code", "unknown") if isinstance(result, dict) else "invalid_json"
        if not isinstance(code, int):
            code = "invalid_response"
        raise TelegramError(f"Telegram API 未成功，錯誤代碼：{code}。")
    if "result" not in result:
        raise TelegramError("Telegram API 回應缺少結果。")
    return result["result"]


def send_text(token: str, chat_id: int, text: str) -> dict:
    """只送一次純文字；失敗或結果不明均不重試。"""
    if type(chat_id) is not int or chat_id <= 0:
        raise TelegramError("發送目的地必須是已配對的私人聊天室。")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
        raise TelegramError("文字不得空白或超過 4096 個 UTF-16 單位；本階段不自動拆分。")
    result = _request_api(token, "sendMessage", {
        "chat_id": chat_id, "text": text,
        "link_preview_options": {"is_disabled": True},
    })
    if (not isinstance(result, dict)
            or not isinstance(result.get("chat"), dict)
            or result["chat"].get("id") != chat_id
            or type(result.get("message_id")) is not int):
        raise TelegramError("Telegram 發送回應無法完整核對；可能已送出，已停止，請先查看手機，勿直接重送。")
    actual = result.get("text")
    if actual != text:
        # 官方 API 現場已驗證會去掉尾端 ASCII 空白；送出內容仍保留原文。
        # 僅接受這個精確差異，不放寬正文、內部空白、換行或其他字元的核對。
        if isinstance(actual, str) and text.endswith(" ") and actual == text.rstrip(" "):
            result = dict(result)
            result["_line_stock_trimmed_trailing_spaces"] = len(text) - len(actual)
        else:
            raise TelegramError("Telegram 發送回應無法完整核對；可能已送出，已停止，請先查看手機，勿直接重送。")
    return result


def send_image(token: str, chat_id: int, png: bytes, caption: str = "") -> dict:
    """以官方 sendDocument 上傳 PNG，保留像素；只嘗試一次，不壓縮、不重試。"""
    from image_probe import decode_public_image, MAX_IMAGE_BYTES

    if type(chat_id) is not int or chat_id <= 0:
        raise TelegramError("發送目的地必須是已配對的私人聊天室。")
    if not isinstance(caption, str) or len(caption.encode("utf-16-le")) // 2 > 1024:
        raise TelegramError("圖片說明不得超過 1024 個 UTF-16 單位。")
    if not isinstance(png, bytes) or not png or len(png) > MAX_IMAGE_BYTES:
        raise TelegramError("PNG 資料空白或超過 40 MiB 上限。")
    # CF_DIB 必須先由圖片探測器轉為 PNG，不能僅改副檔名。
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TelegramError("圖片資料不是 PNG，已停止。")
    try:
        decode_public_image(png, "PNG")
    except Exception:
        raise TelegramError("PNG 圖片無法完整解碼，已停止。") from None

    boundary = "line-stock-" + secrets.token_hex(24)
    chunks = []
    for name, value in {"chat_id": str(chat_id), "caption": caption,
                        "disable_content_type_detection": "true"}.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                       f'\r\n\r\n{value}\r\n').encode("utf-8"))
    chunks.extend([
        (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
         'filename="line-image.png"\r\nContent-Type: image/png\r\n\r\n').encode("ascii"),
        png, f"\r\n--{boundary}--\r\n".encode("ascii"),
    ])
    result = _request_bytes(token, "sendDocument", b"".join(chunks),
                            f"multipart/form-data; boundary={boundary}", timeout=45)
    document = result.get("document") if isinstance(result, dict) else None
    if (not isinstance(result, dict)
            or not isinstance(result.get("chat"), dict)
            or result["chat"].get("id") != chat_id
            or type(result.get("message_id")) is not int
            or not isinstance(document, dict)
            or not isinstance(document.get("file_id"), str) or not document["file_id"]
            or document.get("file_size") != len(png)
            or document.get("file_name") != "line-image.png"
            or result.get("caption", "") != caption):
        raise TelegramError("Telegram 圖片發送回應無法完整核對；可能已送出，已停止，請先查看手機，勿直接重送。")
    return result


def store_token(token: str, bot_id: int) -> str:
    import win32cred
    target = f"line-stock-forwarder/telegram/{bot_id}"
    win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC,
                        "TargetName": target, "UserName": str(bot_id),
                        "CredentialBlob": validate_token(token),
                        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
    return target


def load_token(target: str) -> str:
    import win32cred
    if not re.fullmatch(r"line-stock-forwarder/telegram/[0-9]+", target):
        raise TelegramError("本專案的 Telegram 認證名稱不正確。")
    try:
        data = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        blob = data["CredentialBlob"]
        return validate_token(blob.decode("utf-16-le") if isinstance(blob, bytes) else blob)
    except Exception:
        raise TelegramError("讀不到本專案的 Telegram 認證，請重新執行 setup-telegram.bat。") from None
