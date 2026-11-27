"""Comment-to-DM: when someone comments the promo keyword on a recent post, send them ONE private reply (a DM).
Meta allows one private reply per comment within 7 days. Dex never posts a public reply to a comment."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from app import clock
from app.core import Dex
from app.services.composio_client import ComposioError
from app.services.promo import active_promo, keyword_hit

log = logging.getLogger(__name__)
WINDOW = timedelta(days=7)
MAX_PER_RUN = 25


def _items(res: Any) -> list[dict[str, Any]]:
    """Composio double-wraps Graph lists ({data: {data: [...]}}) on some tools."""
    while isinstance(res, dict) and "data" in res:
        res = res["data"]
    return res if isinstance(res, list) else []


class CommentWatcher:
    def __init__(self, dex: Dex, *, ig_user_id: str, page_id: str):
        self.dex, self.ig_user_id, self.page_id = dex, ig_user_id, page_id
        self.c = dex.dm.c

    async def run_once(self) -> int:
        if self.dex.paused:
            return 0
        promo = active_promo(await self.dex.crm.get_settings())
        if not promo or not promo["keyword"] or not promo["dm"]:
            return 0
        since = clock.now() - WINDOW
        sent = 0
        for channel, fetch in (("instagram", self._ig_comments), ("messenger", self._fb_comments)):
            try:
                comments = await fetch(since)
            except ComposioError as e:
                log.warning("comment check failed on %s: %s", channel, e)
                continue
            for cm in comments:
                if sent >= MAX_PER_RUN:
                    return sent
                if await self._handle(channel, cm, promo):
                    sent += 1
        return sent

    async def _ig_comments(self, since) -> list[dict[str, Any]]:
        media = _items(await self.c.execute("INSTAGRAM_GET_IG_USER_MEDIA", {
            "ig_user_id": "me", "limit": 10, "since": int(since.timestamp()),
            "fields": "id,timestamp,comments_count"}))
        out = []
        for m in media:
            if not m.get("comments_count"):
                continue
            for c in _items(await self.c.execute("INSTAGRAM_GET_IG_MEDIA_COMMENTS", {
                    "ig_media_id": m["id"], "limit": 50, "fields": "id,text,timestamp,from"})):
                frm = c.get("from") or {}
                out.append({"id": c.get("id"), "text": c.get("text", ""), "at": c.get("timestamp"),
                            "from_id": str(frm.get("id", "")), "name": frm.get("username", "")})
        return out

    async def _fb_comments(self, since) -> list[dict[str, Any]]:
        posts = _items(await self.c.execute("FACEBOOK_GET_PAGE_POSTS", {
            "page_id": self.page_id, "limit": 10, "since": str(int(since.timestamp())),
            "fields": "id,created_time,comments.summary(true).limit(0)"}))
        out = []
        for p in posts:
            if not ((p.get("comments") or {}).get("summary") or {}).get("total_count"):
                continue
            for c in _items(await self.c.execute("FACEBOOK_GET_COMMENTS", {
                    "object_id": p["id"], "limit": 50, "fields": "id,message,created_time,from"})):
                frm = c.get("from") or {}
                out.append({"id": c.get("id"), "text": c.get("message", ""), "at": c.get("created_time"),
                            "from_id": str(frm.get("id", "")), "name": frm.get("name", "")})
        return out

    async def _handle(self, channel: str, cm: dict[str, Any], promo: dict[str, str]) -> bool:
        db, kw = self.dex.db, promo["keyword"]
        if not cm.get("id") or db.is_event_processed(f"comment:{cm['id']}"):
            return False
        if cm["from_id"] in (self.ig_user_id, self.page_id) or not keyword_hit(cm["text"], kw):
            db.mark_event_processed(f"comment:{cm['id']}", "comment")
            return False
        try:
            at = clock.parse_iso(str(cm["at"]).replace("+0000", "+00:00"))
            if clock.now() - at > WINDOW:
                db.mark_event_processed(f"comment:{cm['id']}", "comment")
                return False
        except (ValueError, TypeError):
            pass
        person = f"promo:{kw.lower()}:{channel}:{cm['from_id']}"
        customer = await self.dex.crm.find_customer(channel, cm["from_id"]) if cm["from_id"] else None
        if db.kv_get(person) or self.dex.crm.is_dnc(customer):
            db.mark_event_processed(f"comment:{cm['id']}", "comment")
            return False
        db.mark_event_processed(f"comment:{cm['id']}", "comment")  # before sending: never DM twice
        who = cm["name"] or "someone"
        try:
            rid, _ = await self.dex.dm.send_private_reply(channel, cm["id"], promo["dm"])
        except ComposioError as e:
            await self.dex.owner.notify(f"⚠️ Couldn't DM {who} about their \"{kw}\" comment: {e}. "
                                        "You may need to message them yourself.")
            return False
        db.kv_set(person, True)
        db.kv_set(f"promo_dm_to:{channel}:{rid}", {"keyword": kw, "text": promo["dm"], "at": clock.now().isoformat()})
        label = "Instagram" if channel == "instagram" else "Facebook"
        self.dex.activity.log("Dex", f"{who} commented “{kw}” on {label}, so Dex sent them the offer in a DM",
                              f"Sent {who} the {kw} offer")
        await self.dex.owner.notify(f"💬 {who} commented \"{kw}\" on {label}. Dex DMed them the offer.")
        return True
