"""Sending DMs through Composio (PRD Step 10). Every message ID Dex sends is recorded before anything else so the
poller never mistakes Dex's own messages for the owner's (FR-M9)."""

from __future__ import annotations

from app.db import DB
from app.services.composio_client import ComposioError, ComposioLike


class HumanAgentNotAllowed(Exception):
    """Raised when code tries to use the HUMAN_AGENT tag outside the approved paths (FR-H4)."""


class DMClient:
    def __init__(self, composio: ComposioLike, db: DB, page_id: str):
        self.c = composio
        self.db = db
        self.page_id = page_id

    async def send_text(self, channel: str, recipient_id: str, text: str, *, human_agent: bool = False,
                        _approved_path: bool = False) -> str:
        if human_agent and not _approved_path:
            raise HumanAgentNotAllowed("HUMAN_AGENT is only for owner-approved late replies")
        thread_id = f"{channel}:{recipient_id}"
        # Mark the text as in-flight so a poll that races this send doesn't read it as the owner's message.
        pending = self.db.kv_get(f"pending_send:{thread_id}", [])
        self.db.kv_set(f"pending_send:{thread_id}", (pending + [text.strip()])[-5:])
        if channel == "instagram":
            if human_agent:
                data = await self.c.proxy("instagram", "/me/messages", "POST", {
                    "recipient": {"id": recipient_id}, "messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT",
                    "message": {"text": text}})
            else:
                data = await self.c.execute("INSTAGRAM_SEND_TEXT_MESSAGE", {"recipient_id": recipient_id,
                                                                            "text": text})
        elif channel == "messenger":
            args = {"page_id": self.page_id, "recipient_id": recipient_id, "message_text": text,
                    "messaging_type": "MESSAGE_TAG" if human_agent else "RESPONSE"}
            if human_agent:
                args["tag"] = "HUMAN_AGENT"
            data = await self.c.execute("FACEBOOK_SEND_MESSAGE", args)
        else:
            raise ValueError(channel)
        mid = str(data.get("message_id") or data.get("id") or "")
        if not mid:
            raise ComposioError(f"No message id in send response: {data}")
        self.db.record_sent(mid, thread_id)
        return mid

    async def send_private_reply(self, channel: str, comment_id: str, text: str) -> tuple[str, str]:
        """Meta's private reply: one DM to the person who wrote comment_id (within 7 days). Returns
        (recipient_id, message_id). The message is recorded as Dex's so the poller never reads it as the owner's."""
        endpoint = "/me/messages" if channel == "instagram" else f"/{self.page_id}/messages"
        data = await self.c.proxy("instagram" if channel == "instagram" else "facebook", endpoint, "POST", {
            "recipient": {"comment_id": comment_id}, "message": {"text": text}})
        rid, mid = str(data.get("recipient_id") or ""), str(data.get("message_id") or "")
        if not mid:
            raise ComposioError(f"No message id in private reply response: {data}")
        if rid:
            thread_id = f"{channel}:{rid}"
            self.db.kv_set(f"pending_send:{thread_id}", (self.db.kv_get(f"pending_send:{thread_id}", []) + [text])[-5:])
            self.db.record_sent(mid, thread_id)
        return rid, mid
