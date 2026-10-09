"""解析已實測的 LINE 複製格式；僅供受控測試，無法辨別所有媒體類型。"""
from dataclasses import dataclass
from datetime import date
import re


class UnsupportedRecallError(ValueError):
    """公開複製文字中的收回提示無法與相同格式的多行正文可靠區分。"""


class UnsupportedImageContinuationError(ValueError):
    """多張圖片的複製格式可能省略姓名，不能猜測發送者或圖片位置。"""


@dataclass(frozen=True)
class Message:
    day: str
    clock: str
    sender: str
    text: str


@dataclass(frozen=True)
class RecallNotice:
    day: str
    clock: str
    sender: str
    line_number: int


def parse_transcript(text: str, senders: list[str], *, skip_recall_notices: bool = False,
                     notices: list[RecallNotice] | None = None) -> list[Message]:
    if type(skip_recall_notices) is not bool:
        raise ValueError("skip_recall_notices 必須是 true 或 false。")
    if (not isinstance(senders, list) or not senders
            or any(not isinstance(s, str) or not s.strip() or "\n" in s or "\r" in s for s in senders)
            or len(set(senders)) != len(senders)):
        raise ValueError("test_senders 必須設定不同且非空白的測試帳號顯示名稱。")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines[-1] == "":
        lines.pop()  # 聊天匯出最後的分隔換行。
    messages = []
    day = None
    header = None
    body = []
    recall_count = 0

    def finish():
        if header is not None:
            messages.append(Message(header[0], header[1], header[2], "\n".join(body)))

    for line_number, line in enumerate(lines, start=1):
        day_match = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2}) 星期[一二三四五六日天]", line)
        if day_match:
            finish()
            header, body = None, []
            day = date(*map(int, day_match.groups())).isoformat()
            continue
        time_match = re.match(r"^([0-2]\d:[0-5]\d) (.*)$", line)
        if time_match:
            clock, remainder = time_match.groups()
            if day is None or int(clock[:2]) > 23:
                raise ValueError("日期或時間格式無法確認，已停止。")
            candidates = [s for s in senders if remainder.startswith(s + " ")]
            if len(candidates) != 1:
                if remainder == '圖片':
                    raise UnsupportedImageContinuationError(
                        f'第 {line_number} 行圖片提示沒有發送者姓名；可能為連續圖片格式，'
                        '目前無法可靠對應每张圖片，已停止；不猜測、不略過。')
                if any(remainder == s + "已收回訊息" for s in senders):
                    if skip_recall_notices:
                        # 使用者選擇的文字格式規則，不能視為可靠的系統事件辨識。
                        sender = next(s for s in senders if remainder == s + "已收回訊息")
                        recall_count += 1
                        if notices is not None:
                            notices.append(RecallNotice(day, clock, sender, line_number))
                        continue
                    raise UnsupportedRecallError(
                        f"第 {line_number} 行出現 LINE 收回訊息提示；目前不支援此格式，"
                        "無法可靠區分系統提示與多行文字，已停止；本批尚未發送。"
                    )
                raise ValueError("訊息發送者不在唯一的測試帳號清單內，或多行內容與訊息標頭混淆，已停止。")
            sender = candidates[0]
            finish()
            header = (day, clock, sender)
            body = [remainder[len(sender) + 1:]]
        elif header is not None:
            body.append(line)
        else:
            raise ValueError("複製內容中出現無法歸屬的文字行，已停止。")
    finish()
    if not messages and not recall_count:
        raise ValueError("複製內容沒有可辨識的訊息，已停止。")
    return messages


def new_messages(previous: str, current: str, senders: list[str], *, skip_recall_notices: bool = False,
                 notices: list[RecallNotice] | None = None) -> list[Message]:
    from clipboard_probe import appended_text
    appended_text(previous, current)  # 先確認完整原始起點沒有被改寫／裁切。
    old_notices, latest_notices = [], []
    old = parse_transcript(previous, senders, skip_recall_notices=skip_recall_notices, notices=old_notices)
    latest = parse_transcript(current, senders, skip_recall_notices=skip_recall_notices, notices=latest_notices)
    if latest[:len(old)] != old:
        raise ValueError("先前訊息的邊界或內容變更，已停止；不猜測新舊訊息。")
    if latest_notices[:len(old_notices)] != old_notices:
        raise ValueError("先前收回提示的位置變更，已停止。")
    if notices is not None:
        notices.extend(latest_notices[len(old_notices):])
    return latest[len(old):]
