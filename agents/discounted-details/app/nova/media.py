"""Media handling for Nova: Telegram download, privacy blur, Instagram-safe sizing, video frames, public hosting."""

from __future__ import annotations

import asyncio
import io
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageFilter, ImageOps

MAX_BOT_DOWNLOAD = 20 * 1024 * 1024  # Telegram Bot API getFile limit
FEED_MIN_RATIO, FEED_MAX_RATIO = 0.8, 1.91  # Instagram feed aspect limits (4:5 .. 1.91:1)
LONG_SIDE = 1440


class MediaError(Exception):
    pass


@dataclass
class Box:
    """Normalized 0..1000 coordinates (x0, y0, x1, y1), as returned by the vision step."""
    x0: float
    y0: float
    x1: float
    y1: float


async def telegram_download(token: str, file_id: str, client: httpx.AsyncClient | None = None) -> bytes:
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        r = await client.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id})
        data = r.json()
        if not data.get("ok"):
            desc = str(data.get("description", ""))
            if "too big" in desc.lower():
                raise MediaError("FILE_TOO_BIG")
            raise MediaError(desc or "getFile failed")
        info = data["result"]
        if int(info.get("file_size") or 0) > MAX_BOT_DOWNLOAD:
            raise MediaError("FILE_TOO_BIG")
        f = await client.get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}")
        f.raise_for_status()
        return f.content
    finally:
        if own:
            await client.aclose()


def open_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # phone photos carry rotation in EXIF
    return img.convert("RGB")


def to_jpeg(img: Image.Image, quality: int = 88) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def blur_boxes(img: Image.Image, boxes: list[Box], pad: float = 0.08) -> Image.Image:
    """Subtle blur over each box (padded a little so edges of a plate don't show)."""
    img = img.copy()
    w, h = img.size
    for b in boxes:
        bw, bh = (b.x1 - b.x0) / 1000 * w, (b.y1 - b.y0) / 1000 * h
        x0 = max(0, int(b.x0 / 1000 * w - bw * pad))
        y0 = max(0, int(b.y0 / 1000 * h - bh * pad))
        x1 = min(w, int(b.x1 / 1000 * w + bw * pad))
        y1 = min(h, int(b.y1 / 1000 * h + bh * pad))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        region = img.crop((x0, y0, x1, y1))
        radius = max(6, int(min(x1 - x0, y1 - y0) / 4))
        img.paste(region.filter(ImageFilter.GaussianBlur(radius)), (x0, y0))
    return img


def fit_feed(img: Image.Image) -> Image.Image:
    """Center-crop into Instagram's allowed feed aspect range and cap the size."""
    w, h = img.size
    ratio = w / h
    if ratio < FEED_MIN_RATIO:
        new_h = int(w / FEED_MIN_RATIO)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif ratio > FEED_MAX_RATIO:
        new_w = int(h * FEED_MAX_RATIO)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    return cap_size(img)


def cap_size(img: Image.Image, long_side: int = LONG_SIDE) -> Image.Image:
    if max(img.size) > long_side:
        img = img.copy()
        img.thumbnail((long_side, long_side), Image.LANCZOS)
    return img


def for_vision(data: bytes) -> bytes:
    """Smaller JPEG for the vision model (keeps requests light)."""
    return to_jpeg(cap_size(open_image(data), 1280), quality=80)


def video_frames(data: bytes, count: int = 3) -> list[bytes]:
    """Sample frames at 15/50/85% through the clip with ffmpeg (installed in the Docker image)."""
    if not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg not installed")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.mp4"
        src.write_bytes(data)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                "default=nw=1:nk=1", str(src)], capture_output=True, text=True, timeout=60)
        try:
            dur = float(probe.stdout.strip())
        except ValueError:
            dur = 3.0
        frames = []
        for i, frac in enumerate([0.15, 0.5, 0.85][:count]):
            out = Path(d) / f"f{i}.jpg"
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{dur * frac:.2f}", "-i", str(src), "-frames:v", "1",
                            "-q:v", "3", str(out)], capture_output=True, timeout=60)
            if out.exists():
                frames.append(out.read_bytes())
        if not frames:
            raise MediaError("could not read video frames")
        return frames


class MediaHost:
    """Stores processed media under unguessable names so Instagram/Facebook/Telegram can fetch it."""

    def __init__(self, root: str, public_base_url: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.base = public_base_url.rstrip("/")

    def save(self, data: bytes, ext: str) -> str:
        name = f"{secrets.token_urlsafe(24)}.{ext}"
        (self.root / name).write_bytes(data)
        return name

    def url(self, name: str) -> str:
        return f"{self.base}/media/{name}"

    def path(self, name: str) -> Path | None:
        if "/" in name or ".." in name:
            return None
        p = self.root / name
        return p if p.is_file() else None

    def cleanup(self, max_age_days: int = 7, keep: set[str] | frozenset[str] = frozenset()) -> int:
        cutoff = time.time() - max_age_days * 86400
        n = 0
        for p in self.root.iterdir():
            if p.is_file() and p.name not in keep and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                n += 1
        return n


async def run_blocking(fn, *args):
    return await asyncio.to_thread(fn, *args)
