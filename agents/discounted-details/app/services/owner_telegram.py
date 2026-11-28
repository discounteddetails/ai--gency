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

    async def send_card(self, html_text: str, keyboard: list[list[dict[str, Any]]]) -> int | None:
        """HTML card with raw inline buttons (copy_text / url / callback_data). Falls back to plain text."""
        payload = {"chat_id": self.chat_id, "text": html_text[:MAX_LEN], "parse_mode": "HTML",
                   "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": keyboard}}
        res = await self._call("sendMessage", payload)
        if not res.get("ok"):  # e.g. an older Telegram without copy buttons: drop them and retry
            kb = [[b for b in row if "copy_text" not in b] for row in keyboard]
            res = await self._call("sendMessage", {**payload, "reply_markup": {"inline_keyboard": [r for r in kb if r]}})
        return (res.get("result") or {}).get("message_id")

    async def edit_buttons(self, message_id: int, text: str, buttons: list[list[tuple[str, str]]]) -> None:
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in buttons]}
        await self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_LEN],
                                             "reply_markup": markup})

    async def mark_decided(self, message_id: int, text: str) -> None:
        await self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text[:MAX_LEN]})

    async def ask_for_text(self, prompt: str) -> int | None:
        return await self._send(prompt, {"force_reply": True})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def post_media(self, kind: str, urls: list[str], caption: str,
                         buttons: list[list[tuple[str, str]]] | None) -> int | None:
        """Preview photos/videos (Telegram fetches them from our public /media URLs). Albums can't carry buttons."""
        markup = ({"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in buttons]}
                  if buttons else None)
        if len(urls) == 1:
            method, field = ("sendVideo", "video") if kind == "video" else ("sendPhoto", "photo")
            payload: dict[str, Any] = {"chat_id": self.chat_id, field: urls[0], "caption": caption[:1024]}
            if markup:
                payload["reply_markup"] = markup
            res = await self._call(method, payload)
            if not res.get("ok"):  # fall back to a text card so the owner can still decide
                return await self._send(caption + "\n(preview couldn't load)", markup)
            return (res.get("result") or {}).get("message_id")
        media = [{"type": "video" if kind == "video" else "photo", "media": u} for u in urls[:10]]
        media[0]["caption"] = caption[:1024]
        res = await self._call("sendMediaGroup", {"chat_id": self.chat_id, "media": media})
        if markup:
            return await self._send(caption, markup)
        return None

    async def send_document(self, url: str, caption: str, filename: str = "") -> None:
        """Full-quality file (Telegram fetches it from our /media URL)."""
        res = await self._call("sendDocument", {"chat_id": self.chat_id, "document": url, "caption": caption[:1024]})
        if not res.get("ok"):
            await self._send(f"{caption}\n{url}")

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

    async def send_document(self, url, caption, filename="") -> None:
        self._add(kind="document", text=caption, media=[url])

    async def edit_buttons(self, message_id, text, buttons) -> None:
        self._add(kind="edit_buttons", target=message_id, text=text, buttons=buttons)

    async def send_card(self, html_text, keyboard) -> int:
        return self._add(kind="card", text=html_text, keyboard=keyboard)

    async def ask_for_text(self, prompt) -> int:
        return self._add(kind="ask", text=prompt)

    async def answer_callback(self, callback_id, text="") -> None:
        self._add(kind="callback_answer", text=text)

    async def flush_batch(self) -> None:
        return None

    async def post_media(self, kind, urls, caption, buttons) -> int:
        if buttons:
            return self._add(kind="buttons", text=caption, buttons=buttons, media=urls, media_kind=kind)
        return self._add(kind="media", text=caption, media=urls, media_kind=kind)

    def texts(self, kind: str | None = None) -> list[str]:
        return [m["text"] for m in self.messages if kind is None or m["kind"] == kind]
