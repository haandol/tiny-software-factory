"""업로드 이미지 정규화: EXIF 방향 보정, 투명 배경을 흰색으로, RGB, 긴 변을 MAX_IMAGE_SIDE 이하로 줄인 PNG."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from .config import MAX_IMAGE_SIDE


def normalize_image(raw: bytes, out: Path) -> tuple[int, int]:
    """업로드 이미지를 방향 보정·RGB·최대 크기 제한한 PNG로 저장한다."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    if max(img.size) > MAX_IMAGE_SIDE:
        s = MAX_IMAGE_SIDE / max(img.size)
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    img.save(out, "PNG")
    return img.width, img.height
