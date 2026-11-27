"""Owner channel: private Telegram bot (PRD §9.4). Alerts, approval buttons, prompts for typed input."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)
MAX_LEN = 4000


class OwnerChannel(Protocol):
    async def notify(self, text: str, *, urgent: bool = False) -> int | None: ...
    async def post_buttons(self, text: str, buttons: list[list[tuple[str, str]]]) -> int | None: ...
    async def mark_decided(self, message_id: int, text: str) -> None: ...
    async def ask_for_text(self, prompt: str) -> int | None: ...
    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...


class TelegramOwner:
    def __init__(self, token: str, chat_id: int, client: httpx.AsyncClient | None = None,
                 max_alerts_per_hour: int = 20):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id
        self._http = client or httpx.AsyncClient(timeout=15)
        self._sent: list[float] = []
        self._batch: list[str] = []
        self._max = max_alerts_per_hour

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(3):
            try:
                r = await self._http.post(self._base + method, json=payload)
            except httpx.HTTPError as e:
                log.warning("telegram %s transport error: %s", method, e)
                await asyncio.sleep(2 ** attempt)
                continue
            data = r.json() if r.content else {}
            if r.status_code == 429:
                await asyncio.sleep(int(data.get("parameters", {}).get("retry_after", 3)))
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            if not data.get("ok"):
                log.error("telegram %s failed: %s", method, data.get("description"))
                if r.status_code == 400 and "reply_markup" not in payload and payload.get("parse_mode"):
                    payload.pop("parse_mode")
                    continue
            return data
        return {}

    async def _send(self, text: str, reply_markup: dict[str, Any] | None = None) -> int | None:
        msg_id = None
        for i in range(0, max(len(text), 1), MAX_LEN):
            payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text[i:i + MAX_LEN],
                                       "disable_web_page_preview": True}
            if reply_markup and i + MAX_LEN >= len(text):
                payload["reply_markup"] = reply_markup
            res = await self._call("sendMessage", payload)
            msg_id = (res.get("result") or {}).get("message_id", msg_id)
        return msg_id

    async def notify(self, text: str, *, urgent: bool = False) -> int | None:
        """Alerts are rate-limited to N/hour; overflow is batched and flushed by flush_batch() (FR-A4)."""
        now = time.monotonic()
        self._sent = [t for t in self._sent if now - t < 3600]
        if not urgent and len(self._sent) >= self._max:
            self._batch.append(text)
            return None
        self._sent.append(now)
        return await self._send(text)

    async def flush_batch(self) -> None:
        if self._batch:
            items, self._batch = self._batch, []
            await self._send("🗂 Batched alerts:\n\n" + "\n\n".join(items))

    async def post_buttons(self, text: str, buttons: list[list[tuple[str, str]]]) -> int | None:
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in buttons]}
        return await self._send(text, markup)

    async def mark_decided(self, message_id: int, text: str) -> None:
        await self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_LEN]})

    async def ask_for_text(self, prompt: str) -> int | None:
        return await self._send(prompt, {"force_reply": True})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def send_to(self, chat_id: int, text: str) -> None:
        await self._call("sendMessage", {"chat_id": chat_id, "text": text})


class FakeOwner:
    """Records everything for tests."""

    def __init__(self):
        self.messages: list[dict[str, Any]] = []
        self._n = 0

    def _add(self, **kw) -> int:
        self._n += 1
        self.messages.append({"id": self._n, **kw})
        return self._n

    async def notify(self, text: str, *, urgent: bool = False) -> int:
        return self._add(kind="notify", text=text)

    async def post_buttons(self, text, buttons) -> int:
        return self._add(kind="buttons", text=text, buttons=buttons)

    async def mark_decided(self, message_id, text) -> None:
        self._add(kind="edit", target=message_id, text=text)

    async def ask_for_text(self, prompt) -> int:
        return self._add(kind="ask", text=prompt)

    async def answer_callback(self, callback_id, text="") -> None:
        self._add(kind="callback_answer", text=text)

    async def flush_batch(self) -> None:
        return None

    def texts(self, kind: str | None = None) -> list[str]:
        return [m["text"] for m in self.messages if kind is None or m["kind"] == kind]
