"""Offer Story graphic: one 1080x1920 image with the owner's deal written on it, over a darkened job photo."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.nova.media import to_jpeg

W, H = 1080, 1920
FONT = str(Path(__file__).parent / "fonts" / "DejaVuSans-Bold.ttf")
GOLD, WHITE, INK, MINT = (255, 210, 122), (255, 255, 255), (23, 18, 31), (95, 211, 165)
SIDE = 70


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size)


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, start: int, min_size: int = 40) -> ImageFont.FreeTypeFont:
    size = start
    while size > min_size and draw.textlength(text, font=_font(size)) > max_w:
        size -= 4
    return _font(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def _background(photo: bytes | None) -> Image.Image:
    if photo:
        img = ImageOps.fit(Image.open(io.BytesIO(photo)).convert("RGB"), (W, H), Image.LANCZOS)
        img = img.filter(ImageFilter.GaussianBlur(3))
    else:
        img = Image.new("RGB", (W, H), INK)
    shade = Image.new("RGB", (W, H), INK)
    return Image.blend(img, shade, 0.62)


def story_graphic(photo: bytes | None, *, title: str, headline: list[str], lines: list[str], cta: str,
                  brand: str = "DISCOUNTED DETAILS") -> bytes:
    img = _background(photo)
    d = ImageDraw.Draw(img)
    max_w = W - 2 * SIDE

    def centered(y: int, text: str, font, fill) -> int:
        box = d.textbbox((0, 0), text, font=font)
        d.text(((W - (box[2] - box[0])) / 2 - box[0], y - box[1]), text, font=font, fill=fill)
        return y + (box[3] - box[1])

    y = 300  # stay clear of Instagram's top bar
    y = centered(y, brand, _fit(d, brand, max_w, 44), (255, 255, 255, 200)) + 70
    y = centered(y, title.upper(), _fit(d, title.upper(), max_w, 120), GOLD) + 30
    d.rectangle([(W / 2 - 90, y), (W / 2 + 90, y + 8)], fill=GOLD)
    y += 70
    for i, h in enumerate(headline[:2]):
        y = centered(y, h.upper(), _fit(d, h.upper(), max_w, 230 if i == 0 else 110), WHITE) + 36
    y += 40
    body = _font(50)
    for line in lines[:4]:
        for part in _wrap(d, line, body, max_w):
            y = centered(y, part, body, WHITE) + 22
        y += 14
    # call to action pill, above Instagram's bottom reply bar
    font = _fit(d, cta.upper(), max_w - 100, 64)
    box = d.textbbox((0, 0), cta.upper(), font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    py = max(y + 60, H - 520)
    d.rounded_rectangle([((W - tw) / 2 - 50, py), ((W + tw) / 2 + 50, py + th + 70)], radius=(th + 70) // 2, fill=MINT)
    d.text(((W - tw) / 2 - box[0], py + 35 - box[1]), cta.upper(), font=font, fill=INK)
    return to_jpeg(img, quality=90)
