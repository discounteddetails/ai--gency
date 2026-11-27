"""Live activity feed for the HQ page: each agent action becomes a row at the top of the Sheet's Activity tab.
Fire-and-forget: logging never slows or breaks the work it describes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import clock

log = logging.getLogger(__name__)
KEEP_ROWS = 500


class Activity:
    def __init__(self, store: Any):
        self.store = store
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()  # keep rows in order
        self._n = 0

    def log(self, agent: str, text: str, short: str = "") -> None:
        row = {"ts": clock.now().isoformat(timespec="seconds"), "agent": agent, "text": text[:300],
               "short": (short or text)[:60]}
        try:
            t = asyncio.get_running_loop().create_task(self._write(row))
        except RuntimeError:
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _write(self, row: dict[str, Any]) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self.store.insert_top, "Activity", row)
                self._n += 1
                if self._n % 50 == 0:
                    await asyncio.to_thread(self.store.trim, "Activity", KEEP_ROWS)
            except Exception as e:
                log.warning("activity log failed: %s", e)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
