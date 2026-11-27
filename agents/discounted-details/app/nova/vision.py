"""Claude vision: classify the owner's photos/frames and locate private details to blur. Caption writing."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

CATEGORIES = ["on_the_way", "mid_job", "before", "after", "promo", "skip"]

BOX = {"type": "object", "additionalProperties": False,
       "properties": {"x0": {"type": "number"}, "y0": {"type": "number"}, "x1": {"type": "number"},
                      "y1": {"type": "number"}},
       "required": ["x0", "y0", "x1", "y1"]}

CLASSIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "index": {"type": "integer"},
                "category": {"type": "string", "enum": CATEGORIES},
                "quality": {"type": "integer", "description": "1-10 how good it would look on Instagram"},
                "vehicle": {"type": "string", "description": "e.g. 'black Chevy Tahoe', or empty"},
                "area": {"type": "string", "enum": ["interior", "exterior", "both", "none"]},
                "plates": {"type": "array", "items": BOX},
                "house_numbers": {"type": "array", "items": BOX},
                "faces": {"type": "array", "items": BOX},
                "other_private": {"type": "string", "description": "street signs, mail, screens with names... or empty"},
                "pair_key": {"type": "string", "description": "same short key for before/after shots of the same spot"},
            },
            "required": ["index", "category", "quality", "vehicle", "area", "plates", "house_numbers", "faces",
                         "other_private", "pair_key"]}},
    },
    "required": ["items"],
}

CLASSIFY_PROMPT = """You review photos for Discounted Details, a mobile car detailing business in Kyle, TX.
Each image is labeled "Item N" (video items show several frames from the same clip). For every item return:
- category: on_the_way (driving/arriving, truck/supplies/road), mid_job (the detailer working: tools in use, foam,
  vacuuming, extraction), before (vehicle still dirty), after (vehicle clean, finished), promo (a flyer, graphic,
  price list or text announcement made for an ad or offer), skip (blurry, duplicate, nothing useful).
- quality 1-10 for how good it would look on Instagram.
- plates / house_numbers / faces: bounding boxes for EVERY license plate, house or address number, and human face,
  in coordinates normalized 0-1000 (x0,y0 top-left, x1,y1 bottom-right) of that image. Make boxes slightly generous.
  For video items, report anything seen in any frame (boxes may be approximate).
- other_private: anything else identifying (street signs, mail, names on screens), or "".
- pair_key: give before and after shots of the same seat/area/angle the same short key (e.g. "rear-seat").
Return JSON only."""

CAPTION_SCHEMA = {"type": "object", "additionalProperties": False,
                  "properties": {"caption": {"type": "string"}}, "required": ["caption"]}

CAPTION_PROMPT = """Write an Instagram/Facebook caption for a Discounted Details before/after post.
Rules (all required):
- About THIS specific car and job; never a generic template. Professional and friendly, 1-3 emojis.
- Mention the city only (never a street, address, neighborhood or the customer's name).
- {price_rule} Never the words perfect, flawless, guarantee or 100%. ("new" is fine.)
- End with a call to DM, e.g. "DM 'DETAIL' for a price on yours".
- Exactly 5 hashtags at the very end that fit the city and mobile detailing (e.g. #kyletx #buda #austindetailing
  #mobiledetailing #cardetailing).
- Under 600 characters before the hashtags.
Recent captions (do NOT repeat their wording):
{recent}
Job facts:
{facts}"""


def _img_block(jpeg: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode()}}


class Vision:
    def __init__(self, client, model: str = "claude-opus-5-5"):
        self.client = client
        self.model = model

    async def _json(self, content: list[dict[str, Any]], schema: dict[str, Any], effort: str = "medium") -> dict:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=8000, messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
        if resp.stop_reason == "refusal":
            raise RuntimeError("vision request refused")
        text = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return json.loads(text)

    async def classify(self, items: list[list[bytes]]) -> list[dict[str, Any]]:
        """items[i] is a list of JPEGs (1 for a photo, several frames for a video)."""
        content: list[dict[str, Any]] = [{"type": "text", "text": CLASSIFY_PROMPT}]
        for i, frames in enumerate(items):
            label = f"Item {i}" + (f" (video, {len(frames)} frames)" if len(frames) > 1 else "")
            content.append({"type": "text", "text": label})
            content += [_img_block(f) for f in frames]
        data = await self._json(content, CLASSIFY_SCHEMA)
        by_index = {int(x["index"]): x for x in data.get("items", [])}
        return [by_index.get(i, {"index": i, "category": "skip", "quality": 0, "vehicle": "", "area": "none",
                                 "plates": [], "house_numbers": [], "faces": [], "other_private": "",
                                 "pair_key": ""}) for i in range(len(items))]

    async def caption(self, facts: str, recent: list[str], photos: list[bytes], *, offer: bool = False) -> str:
        price_rule = ("This post is about the owner's offer: state the offer clearly, with its prices and end date "
                      "exactly as the owner's note gives them (never invent a price or a deal)." if offer
                      else "No prices.")
        content = [{"type": "text", "text": CAPTION_PROMPT.format(
            recent="\n".join(f"- {r[:200]}" for r in recent) or "- (none)", facts=facts, price_rule=price_rule)}]
        content += [_img_block(p) for p in photos[:4]]
        return (await self._json(content, CAPTION_SCHEMA, effort="medium"))["caption"].strip()
