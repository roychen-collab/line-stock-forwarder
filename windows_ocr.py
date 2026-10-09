"""以 Windows 公開 OCR API 辨識本機圖像；沒有網路請求。"""
import asyncio


async def _recognize(image):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, BitmapAlphaMode, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine

    languages = [language.language_tag for language in OcrEngine.available_recognizer_languages]
    if "zh-Hant-TW" not in languages:
        raise RuntimeError("Windows 尚未提供繁體中文 OCR 語言；已停止。")
    engine = OcrEngine.try_create_from_language(Language("zh-Hant-TW"))
    if engine is None:
        raise RuntimeError("Windows 繁體中文 OCR 引擎無法建立。")
    if max(image.size) > OcrEngine.max_image_dimension:
        raise ValueError("OCR 圖像超過 Windows 尺寸上限。")
    rgba = image.convert("RGBA")
    pixels = bytearray(rgba.tobytes("raw", "BGRA"))
    bitmap = SoftwareBitmap.create_copy_with_alpha_from_buffer(
        pixels, BitmapPixelFormat.BGRA8, rgba.width, rgba.height, BitmapAlphaMode.IGNORE)
    try:
        result = await engine.recognize_async(bitmap)
    finally:
        bitmap.close()
    lines = []
    for line in result.lines:
        words = []
        for word in line.words:
            rect = word.bounding_rect
            words.append({"text": word.text, "x": rect.x, "y": rect.y,
                          "width": rect.width, "height": rect.height})
        lines.append({"text": line.text, "words": words})
    return {"language": "zh-Hant-TW", "lines": lines, "text": result.text}


def recognize_image(image):
    return asyncio.run(_recognize(image))

