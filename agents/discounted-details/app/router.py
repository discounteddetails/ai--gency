"""Routes the owner's plain Telegram messages to the right agent (Dex, Nova or Pixel)."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

from app import clock

log = logging.getLogger(__name__)
NAMES = re.compile(r"^\s*(dex|nova|pixel)\b[\s,:;!-]*", re.IGNORECASE)

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "agent": {"type": "string", "enum": ["dex", "nova", "pixel"]},
        "action": {"type": "string", "enum": ["owner_command", "new_design", "change_design", "remember_style",
                                              "show_style", "nova_caption", "nova_other"]},
        "text": {"type": "string", "description": "The instruction for that agent, with any greeting/name removed"},
    },
    "required": ["agent", "action", "text"],
}

PROMPT = """The owner of Discounted Details (mobile car detailing) sent a message to their team's Telegram bot.
Pick the agent and action:
- dex / owner_command: customers, DMs, bookings, calendar, quotes, prices, follow-ups, the CRM/Sheet, offers and
  promos for customers, anything about the business that isn't designing or posting.
- pixel (graphic designer): new_design = a request for a new poster/flyer/thumbnail/cover/graphic;
  change_design = feedback about the design they're currently looking at (size, colors, text, layout, "make it
  9:16", "bigger price", "use the other photo"); remember_style = a lasting style preference ("always use navy");
  show_style = asking what Pixel remembers about their style.
- nova (social media poster): nova_caption = new caption text for the post waiting for approval;
  nova_other = anything else about posting photos/Stories/scheduling.
If the message starts with an agent's name, that agent is the one. Use the context: feedback right after Pixel
sent designs is almost always change_design for that design.
Context:
{context}
Message: {message}"""


class Router:
    def __init__(self, client, model: str, *, pixel=None, nova=None, dex=None):
        self.client, self.model, self.pixel, self.nova, self.dex = client, model, pixel, nova, dex

    def context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {}
        if self.pixel and (job := self.active_design()):
            ctx["pixel_current_design"] = {"id": job["job_id"], "title": job["data"].get("title", ""),
                                           "status": job["status"], "sizes": job["data"].get("formats")}
        if self.nova and (post := self.pending_post()):
            ctx["nova_post_waiting_for_approval"] = {"id": post["content_id"],
                                                     "caption": post["data"].get("caption", "")[:200]}
        if self.dex:
            ctx["dex_pending_approvals"] = len(self.dex.db.list_approvals("pending"))
        return ctx

    def active_design(self) -> dict[str, Any] | None:
        rows = self.pixel.dex.db._q("SELECT job_id FROM pixel_jobs WHERE status IN ('choosing','picked','designing') "
                                    "ORDER BY created_at DESC LIMIT 1")
        job = self.pixel.get(rows[0]["job_id"]) if rows else None
        if job and clock.now() - clock.parse_iso(job["data"].get("created_at")) < timedelta(hours=6):
            return job
        return None

    def pending_post(self) -> dict[str, Any] | None:
        posts = self.nova.list(status="pending", kind="post")
        return posts[-1] if posts else None

    async def route(self, message: str) -> dict[str, str]:
        m = NAMES.match(message)
        named = m.group(1).lower() if m else None
        try:
            resp = await self.client.messages.create(
                model=self.model, max_tokens=1000,
                messages=[{"role": "user", "content": PROMPT.format(context=json.dumps(self.context()),
                                                                    message=message)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
            r = json.loads(next(b.text for b in resp.content if getattr(b, "type", "") == "text"))
        except Exception as e:
            log.warning("router failed (%s); sending to %s", e, named or "dex")
            r = {"agent": named or "dex", "action": "owner_command" if (named or "dex") == "dex" else "",
                 "text": message[m.end():] if m else message}
        if named and r["agent"] != named:  # addressing an agent by name always wins
            r["agent"] = named
            r["action"] = {"dex": "owner_command", "pixel": r["action"] if r["action"].endswith(("design", "style"))
                           else "new_design", "nova": "nova_other"}[named]
        r["text"] = (r.get("text") or "").strip() or (message[m.end():] if m else message)
        return r
