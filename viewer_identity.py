"""本機 OCR 標頭核對；嚴格拒絕日期猜測、欄位不一致及同分鐘訊息歧義。"""
from datetime import date
import math
import re

_ENGINE = None


def parse_display_datetime(text: str) -> tuple[str, str]:
    compact = re.sub(r"\s+", "", text).rstrip(">")
    match = re.fullmatch(r"(\d{4})[./](\d{1,2})[./](\d{1,2})(上午|下午)(\d{1,2}):(\d{2})", compact)
    if not match:
        raise ValueError("圖片標頭日期／上午下午格式不完整，禁止猜測。")
    year, month, day, period, hour, minute = match.groups()
    day = date(int(year), int(month), int(day)).isoformat()
    hour, minute = int(hour), int(minute)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError("圖片標頭時間無效。")
    hour = hour % 12 + (12 if period == "下午" else 0)
    return day, f"{hour:02d}:{minute:02d}"


def read_fields(image, dpi: int) -> dict:
    from PIL import ImageOps
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    from windows_ocr import recognize_image

    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR(use_text_det=False, use_angle_cls=False)
    scale, center = dpi / 96, image.width / 2

    def region(half_width, y1, y2):
        return image.crop((round(center - half_width * scale), round(y1 * scale),
                           round(center + half_width * scale), round(y2 * scale)))

    title = region(100, 0, 23)
    title = ImageOps.grayscale(title).point(lambda p: 0 if p >= 200 else 255)
    title = ImageOps.expand(title, border=12, fill=255).resize((title.width * 2 + 48, title.height * 2 + 48))
    group = recognize_image(title)["text"]
    variants = []
    # 姓名門檻 160 會混入淺灰背景，200 又會失去細線；日期保留上午／下午細線。
    for sender_threshold, datetime_threshold in ((180, 160), (190, 180)):
        values = {}
        for name, crop in {"sender": region(100, 32, 51), "datetime": region(65, 51, 69)}.items():
            threshold = sender_threshold if name == "sender" else datetime_threshold
            light = ImageOps.grayscale(crop).point(lambda p: 255 if p >= threshold else 0)
            if name == "sender":
                bounds = light.getbbox()
                if bounds is None:
                    raise ValueError("標頭沒有可辨識的發送者。")
                light = light.crop(bounds)
            processed = ImageOps.expand(ImageOps.invert(light), border=5, fill=255).convert("RGB")
            results, _ = _ENGINE(np.asarray(processed))
            if name == 'sender' and (not results or len(results) != 1 or float(results[0][2]) < 0.75):
                # 繁體姓名可能不在 RapidOCR 的辨識字集中；Windows 提供本機繁中 OCR。
                # 不把錯字換成預期姓名，也不捏造 Windows 沒有提供的信心分數。
                native = recognize_image(ImageOps.expand(processed, border=40, fill='white'))
                if len(native['lines']) != 1 or not native['text'].strip():
                    raise ValueError('Windows 繁中 OCR 沒有唯一的發送者姓名；禁止猜測。')
                values[name] = {'text': native['text'], 'score': None,
                                'engine': 'Windows.OcrEngine', 'line_count': 1}
                continue
            if not results or len(results) != 1:
                raise ValueError("圖片標頭 OCR 沒有唯一的文字結果。")
            values[name] = {"text": results[0][1], "score": float(results[0][2])}
        variants.append(values)
    return {"group": group, "variants": variants}


def verify_fields(fields: dict, group: str, expected, transcript_messages) -> dict:
    # 群組仍須由呼叫端另核對精確 Win32 視窗名稱與官方程序。
    if re.sub(r"\s+", "", fields["group"]) != re.sub(r"\s+", "", group):
        raise ValueError("OCR 群組標頭不相符或已隱藏，禁止擷取。")
    key = (expected.day, expected.clock, expected.sender)
    sender_key = re.sub(r"\s+", "", expected.sender)
    matching_names = {m.sender for m in transcript_messages
                      if re.sub(r"\s+", "", m.sender) == sender_key}
    if len(matching_names) != 1:
        raise ValueError('成員姓名去除 OCR 空格後無法唯一辨識，禁止擷取。')
    if sum((m.day, m.clock, m.sender) == key for m in transcript_messages) != 1:
        raise ValueError("同一發送者在同一分鐘有多則訊息，標頭無法唯一對應；已停止。")
    variants = fields.get("variants", [])
    if len(variants) != 2:
        raise ValueError("OCR 必須取得兩組標頭辨識結果。")
    for variant in variants:
        sender_field = variant['sender']
        if sender_field.get('engine') == 'Windows.OcrEngine':
            if sender_field.get('score') is not None or sender_field.get('line_count') != 1:
                raise ValueError('Windows 姓名辨識證據不完整；禁止猜測。')
            scores = [variant['datetime']['score']]
        elif sender_field.get('engine') in (None, 'RapidOCR'):
            scores = [sender_field['score'], variant['datetime']['score']]
        else:
            raise ValueError('未知姓名辨識引擎，禁止猜測。')
        if any(not math.isfinite(score) or score < 0.75 or score > 1 for score in scores):
            raise ValueError("OCR 分數低於本次測試門檻；禁止猜測。")
        sender = re.sub(r"\s+", "", variant["sender"]["text"])
        if sender != sender_key:
            raise ValueError("OCR 發送者與新增訊息不相符。")
        day, clock = parse_display_datetime(variant["datetime"]["text"])
        if (day, clock) != (expected.day, expected.clock):
            raise ValueError("OCR 日期／時間與新增訊息不相符，可能開到舊圖片。")
    return {"group": group, "day": expected.day, "clock": expected.clock, "sender": expected.sender}
