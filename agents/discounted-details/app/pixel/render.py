"""Render Pixel's HTML designs to images in headless Chromium.
Locked down: JavaScript off and every request blocked except local files under the render folder (no network)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)
FONTS = Path(__file__).parent / "fonts"
FORMATS = {"feed": (1080, 1350), "story": (1080, 1920), "square": (1080, 1080), "youtube": (1280, 720)}

BASE_CSS = """
@font-face{font-family:'Inter';src:url('fonts/Inter.ttf');font-weight:100 900}
@font-face{font-family:'Montserrat';src:url('fonts/Montserrat.ttf');font-weight:100 900}
@font-face{font-family:'Anton';src:url('fonts/Anton.ttf')}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:%(w)dpx;height:%(h)dpx;overflow:hidden;background:#0a1630;font-family:'Inter',sans-serif;
  -webkit-font-smoothing:antialiased}
"""


class RenderError(Exception):
    pass


def _clean(html: str) -> str:
    """Strip scripts, event handlers and anything that could reach outside the render folder."""
    html = re.sub(r"<\s*(script|iframe|object|embed|link|meta|base)\b[^>]*>(.*?<\s*/\s*\1\s*>)?", "", html,
                  flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html, flags=re.IGNORECASE)
    html = re.sub(r"@import[^;]*;", "", html, flags=re.IGNORECASE)
    return html


class Renderer:
    def __init__(self, executable_path: str | None = None):
        self.executable_path = executable_path
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._browser and self._browser.is_connected():
            return self._browser
        from playwright.async_api import async_playwright

        self._pw = self._pw or await async_playwright().start()
        kw = {"executable_path": self.executable_path} if self.executable_path else {}
        self._browser = await self._pw.chromium.launch(args=["--no-sandbox"], **kw)
        return self._browser

    async def render(self, body: str, fmt: str, assets: dict[str, Path]) -> bytes:
        """body: the design's HTML (with its own <style>). Images are written as src="asset:NAME"."""
        if fmt not in FORMATS:
            raise RenderError(f"unknown format {fmt}")
        w, h = FORMATS[fmt]
        async with self._lock:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                shutil.copytree(FONTS, root / "fonts")
                for name, src in assets.items():
                    shutil.copy(src, root / f"asset_{name}{src.suffix}")
                html = _clean(body)
                for name, src in assets.items():
                    html = html.replace(f"asset:{name}", f"asset_{name}{src.suffix}")
                doc = (f"<!doctype html><html><head><meta charset='utf-8'><style>{BASE_CSS % {'w': w, 'h': h}}"
                       f"</style></head><body>{html}</body></html>")
                (root / "index.html").write_text(doc, encoding="utf-8")
                browser = await self._ensure()
                ctx = await browser.new_context(viewport={"width": w, "height": h}, java_script_enabled=False,
                                                device_scale_factor=1)
                try:
                    page = await ctx.new_page()
                    prefix = root.as_uri()

                    async def guard(route):
                        url = route.request.url
                        if url.startswith(prefix) or url.startswith("data:"):
                            await route.continue_()
                        else:
                            await route.abort()

                    await page.route("**/*", guard)
                    await page.goto((root / "index.html").as_uri(), wait_until="load", timeout=30000)
                    png = await page.screenshot(type="png", full_page=False)
                finally:
                    await ctx.close()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
