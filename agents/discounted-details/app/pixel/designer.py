"""Claude designs: each request becomes 2 options, each written as HTML/CSS per output size."""

from __future__ import annotations

import base64
import json
from typing import Any

FORMAT_NAMES = ["feed", "story", "square", "youtube"]

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["poster", "thumbnail", "other"]},
        "title": {"type": "string", "description": "3-6 word name for this design job"},
        "formats": {"type": "array", "items": {"type": "string", "enum": FORMAT_NAMES}},
        "options": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "label": {"type": "string", "description": "2-4 words describing the style"},
                "pages": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"format": {"type": "string", "enum": FORMAT_NAMES},
                                   "html": {"type": "string"}},
                    "required": ["format", "html"]}},
            },
            "required": ["label", "pages"]}},
    },
    "required": ["kind", "title", "formats", "options"],
}

BRIEF = """You are Pixel, the graphic designer for Discounted Details, a one-person mobile car detailing business in
Kyle, TX. You design offer posters and video thumbnails that look professionally made.

BRAND: blues, black and white. Clean and modern. Suggested palette: deep navy #0a1630, royal blue #1d4ed8,
bright blue #3b9bff, ice #e8f2ff, black #05070d, white #ffffff. One accent at most.
FONTS available (nothing else loads): 'Anton' (tall condensed display, great for big prices and thumbnail words),
'Montserrat' (geometric, weights 400-900, headings), 'Inter' (clean body text, 400-800).
{logo_line}

OUTPUT: exactly 2 options in clearly different styles (e.g. bold photo-led vs. clean graphic). For each option,
one page per format listed in `formats`. Formats and sizes (px):
- feed 1080x1350 (Instagram/Facebook post)   - story 1080x1920 (Stories/Reel cover)
- square 1080x1080                           - youtube 1280x720 (YouTube thumbnail)
Choose formats: posters -> ["feed","story"]; thumbnails -> ["youtube","story"] unless the owner asks otherwise.

HTML RULES (the page is screenshotted at exactly that size; no scrolling, no JavaScript):
- Each page's `html` is a fragment: one <style> block, then the markup. body is already width x height.
- Use absolute positioning or flex inside a full-size container. Everything must fit inside the canvas.
- Images ONLY as src="asset:NAME" or url('asset:NAME') using the asset names given below. No other URLs.
- Use object-fit:cover / background-size:cover for photos; add gradient overlays so text is always readable.
- Story pages: keep text out of the top 250px and bottom 340px (Instagram's UI covers them).
- Thumbnails: 2-5 huge words max, very high contrast, readable at phone-thumbnail size; a face-free crop of the
  photo; optional bold outline/stroke or a bright block behind the words.
- Posters: one clear headline, the offer in the biggest type, 1-3 short supporting lines, a call to action.

CONTENT RULES:
- Use only facts in the owner's request (prices, dates, keyword). Never invent prices, deals or dates.
- Never the words perfect, flawless, guarantee or 100%. Never an address or a customer's name.
- If the owner gives exact wording, use it exactly.

ASSETS you can use:
{assets}"""


def _img(jpeg: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode()}}


class Designer:
    def __init__(self, client, model: str):
        self.client, self.model = client, model

    async def design(self, request: str, assets: list[dict[str, Any]], *, has_logo: bool,
                     previous: dict[str, Any] | None = None, feedback: str = "",
                     taste: dict[str, Any] | None = None) -> dict[str, Any]:
        """assets: [{name, desc, preview_jpeg}]. previous/feedback for revisions."""
        asset_lines = "\n".join(f"- asset:{a['name']} — {a['desc']}" for a in assets) or \
            "- (no photos: design with color, shapes and type only)"
        if has_logo:
            asset_lines += "\n- asset:logo — the business logo (PNG, may be transparent)"
        logo_line = ("LOGO: include asset:logo small and tidy on every design." if has_logo
                     else "LOGO: none yet. Set 'DISCOUNTED DETAILS' as a small clean wordmark instead.")
        content: list[dict[str, Any]] = [{"type": "text", "text": BRIEF.format(logo_line=logo_line,
                                                                                assets=asset_lines)}]
        if taste and (taste.get("rules") or taste.get("liked")):
            lines = ["OWNER'S TASTE (learned from what they picked, changed and told you; follow it):"]
            lines += [f"- Rule: {r}" for r in taste.get("rules", [])]
            lines += [f"- They asked for: {f}" for f in taste.get("feedback", [])[-10:]]
            lines.append("Option A MUST closely follow the style of their liked designs below (layout, colors, type "
                         "treatment). Option B can try something new but must still obey every rule.")
            content.append({"type": "text", "text": "\n".join(lines)})
            for i, d in enumerate(taste.get("liked", [])[:3], 1):
                tag = "they posted this one" if d.get("used") else "they picked this one"
                content.append({"type": "text", "text": f"LIKED DESIGN {i} ({d.get('kind')}, “{d.get('label')}”, {tag}). "
                                                        f"Its HTML for {d.get('format')}:\n{d.get('html', '')[:6000]}"})
                if d.get("jpeg"):
                    content.append(_img(d["jpeg"]))
        for a in assets:
            content += [{"type": "text", "text": f"asset:{a['name']}"}, _img(a["preview_jpeg"])]
        if previous:
            content.append({"type": "text", "text": "PREVIOUS DESIGN (the owner picked this one to change):\n"
                                                    + json.dumps(previous)[:30000]})
            content.append({"type": "text", "text": f"OWNER'S CHANGES: {feedback}\nReturn 2 options: the first "
                                                    "applies exactly these changes to the previous design; the "
                                                    "second is a fresh take that also follows them."})
        content.append({"type": "text", "text": f"OWNER'S REQUEST: {request}"})
        resp = await self.client.messages.create(
            model=self.model, max_tokens=16000, messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}})
        if resp.stop_reason == "refusal":
            raise RuntimeError("design request refused")
        text = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(text)
        data["options"] = [o for o in data.get("options", []) if o.get("pages")][:2]
        if not data["options"]:
            raise RuntimeError("no designs returned")
        return data
