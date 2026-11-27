"""Merge rapid-fire messages per thread (FR-M4): wait N seconds after the latest message, then process them together."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, seconds: float, handler: Callable[[str, list[Any]], Awaitable[None]]):
        self.seconds = seconds
        self.handler = handler
        self._buffers: dict[str, list[Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def add(self, thread_id: str, item: Any) -> None:
        self._buffers.setdefault(thread_id, []).append(item)
        if (t := self._tasks.get(thread_id)) and not t.done():
            t.cancel()
        self._tasks[thread_id] = asyncio.create_task(self._fire(thread_id))

    async def _fire(self, thread_id: str) -> None:
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            return
        items = self._buffers.pop(thread_id, [])
        self._tasks.pop(thread_id, None)
        if items:
            try:
                await self.handler(thread_id, items)
            except Exception:
                log.exception("debounced handler failed for %s", thread_id)

    async def drain(self) -> None:
        """Tests: wait for all pending fires."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
