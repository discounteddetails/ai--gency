"""Polls Instagram + Messenger through Composio for new messages (FR-M1, FR-M9)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app import clock
from app.db import DB
from app.services.composio_client import ComposioLike

log = logging.getLogger(__name__)


@dataclass
class InboundEvent:
    channel: str
    sender_id: str
    mid: str
    text: str
    timestamp: datetime
    from_business: bool
    attachments: list[str] = field(default_factory=list)
    sender_name: str = ""

    @property
    def thread_id(self) -> str:
        return f"{self.channel}:{self.sender_id}"


class DMPoller:
    def __init__(self, composio: ComposioLike, db: DB, *, page_id: str, ig_user_id: str,
                 ig_username: str = "discounteddetails", page_name: str = "Discounted Details",
                 on_customer_message: Callable[[InboundEvent], Awaitable[None]],
                 on_owner_message: Callable[[InboundEvent], Awaitable[None]],
                 on_failure_streak: Callable[[int], Awaitable[None]] | None = None):
        self.c = composio
        self.db = db
        self.page_id, self.ig_user_id = str(page_id), str(ig_user_id)
        self.ig_username, self.page_name = ig_username.lower(), page_name.lower()
        self.on_customer = on_customer_message
        self.on_owner = on_owner_message
        self.on_failure_streak = on_failure_streak
        self._lock = asyncio.Lock()

    def _is_business(self, channel: str, who: dict[str, Any]) -> bool:
        if channel == "instagram":
            return str(who.get("id")) == self.ig_user_id or str(who.get("username", "")).lower() == self.ig_username
        return str(who.get("id")) == self.page_id or str(who.get("name", "")).lower() == self.page_name

    async def _list(self, channel: str) -> list[dict[str, Any]]:
        if channel == "instagram":
            d = await self.c.execute("INSTAGRAM_LIST_ALL_CONVERSATIONS",
                                     {"platform": "instagram", "limit": 25, "fields": "id,updated_time,participants"})
        else:
            d = await self.c.execute("FACEBOOK_GET_PAGE_CONVERSATIONS",
                                     {"page_id": self.page_id, "limit": 25, "fields": "id,updated_time,participants"})
        return d.get("data", [])

    async def _messages(self, channel: str, conv_id: str) -> list[dict[str, Any]]:
        fields = "id,created_time,from,to,message,attachments"
        if channel == "instagram":
            d = await self.c.execute("INSTAGRAM_LIST_ALL_MESSAGES",
                                     {"conversation_id": conv_id, "limit": 10, "fields": fields})
        else:
            d = await self.c.execute("FACEBOOK_GET_CONVERSATION_MESSAGES",
                                     {"page_id": self.page_id, "conversation_id": conv_id, "limit": 10,
                                      "fields": fields})
        return d.get("data", [])

    async def poll_once(self) -> int:
        """Returns the number of new events handled. Never overlaps itself."""
        if self._lock.locked():
            return 0
        async with self._lock:
            try:
                n = 0
                for ch in ("instagram", "messenger"):
                    n += await self._poll_channel(ch)
                self.db.kv_set("poll_fail_streak", 0)
                self.db.kv_set("poller_initialized", True)
                return n
            except Exception as e:
                streak = int(self.db.kv_get("poll_fail_streak", 0)) + 1
                self.db.kv_set("poll_fail_streak", streak)
                log.warning("poll failed (%s in a row): %s", streak, e)
                if streak == 5 and self.on_failure_streak:
                    await self.on_failure_streak(streak)
                return 0

    async def _poll_channel(self, channel: str) -> int:
        initialized = bool(self.db.kv_get("poller_initialized", False))
        seen: dict[str, str] = self.db.kv_get(f"conv_seen:{channel}", {})
        handled = 0
        for conv in await self._list(channel):
            cid, updated = conv["id"], conv.get("updated_time", "")
            if seen.get(cid) == updated:
                continue
            prev_seen = seen.get(cid)
            seen[cid] = updated
            if not initialized:
                continue  # first run: record state only, never reply to old messages
            participants = (conv.get("participants") or {}).get("data", [])
            customer = next((p for p in participants if not self._is_business(channel, p)), None)
            if not customer:
                continue
            msgs = await self._messages(channel, cid)
            cutoff = clock.parse_iso(prev_seen) - timedelta(seconds=1) if prev_seen else clock.now() - timedelta(hours=24)
            for m in sorted(msgs, key=lambda x: x.get("created_time", "")):
                mid = str(m.get("id"))
                ts = clock.parse_iso(m["created_time"]) if m.get("created_time") else clock.now()
                if ts < cutoff or self.db.is_event_processed(mid):
                    continue
                self.db.mark_event_processed(mid, channel)
                frm = m.get("from") or {}
                ev = InboundEvent(
                    channel=channel, sender_id=str(customer["id"]), mid=mid, text=str(m.get("message") or ""),
                    timestamp=ts, from_business=self._is_business(channel, frm),
                    attachments=[str(a.get("image_data", {}).get("url") or a.get("file_url") or a.get("name") or "")
                                 for a in ((m.get("attachments") or {}).get("data") or [])],
                    sender_name=str(customer.get("name") or customer.get("username") or ""))
                if ev.from_business:
                    pending = self.db.kv_get(f"pending_send:{ev.thread_id}", [])
                    if self.db.was_sent_by_dex(mid) or ev.text.strip() in pending:
                        continue
                    await self.on_owner(ev)
                else:
                    await self.on_customer(ev)
                handled += 1
        self.db.kv_set(f"conv_seen:{channel}", seen)
        return handled
