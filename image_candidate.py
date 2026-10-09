"""從最新公開訊息列的畫面找唯一大型圖片候選；不依姓名猜左右位置。"""
def locate_image_candidate(image, dpi: int = 96) -> tuple[int, int, int, int]:
    import cv2
    import numpy as np

    scale = dpi / 96
    if not 0.5 <= scale <= 4:
        raise ValueError('來源 DPI 不在已支援範圍，禁止點擊。')
    pixels = np.asarray(image.convert('RGB'))
    # 本次實測 LINE 白色聊天背景；其他主題或大片被覆蓋不套用此辨識。
    if pixels.size == 0 or (pixels.min(axis=2) >= 245).mean() < 0.2:
        raise ValueError('來源圖片列不是已實測的白色背景，禁止猜測位置。')
    mask = (pixels.min(axis=2) < 225).astype('uint8')
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    candidates = []
    for x, y, width, height, area in stats[1:]:
        if (width >= 80 * scale and height >= 80 * scale
                and area >= 5000 * scale * scale and area / (width * height) >= 0.55):
            # 候選必須基本完整顯示；不點被裁掉的大圖。
            if y + height >= image.height - 2:
                raise ValueError('大型圖片候選被視窗底部裁切，禁止點擊。')
            candidates.append((int(x), int(y), int(x + width), int(y + height)))
    if len(candidates) != 1:
        raise ValueError('最新訊息列沒有唯一大型圖片區域，禁止猜測位置。')
    return candidates[0]
