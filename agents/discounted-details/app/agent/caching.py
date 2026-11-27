"""Conversation caching: mark the newest message so the whole history is read from cache on the next step.
Cache reads cost ~5% of normal input. The stored history is never modified (append-only)."""

from __future__ import annotations

import copy
from typing import Any

CACHE = {"type": "ephemeral"}


def with_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of `messages` with a cache breakpoint on the last block of the last message."""
    if not messages:
        return messages
    last = copy.deepcopy(messages[-1])
    content = last.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not content:
        return messages
    block = content[-1]
    if block.get("type") in ("thinking", "redacted_thinking"):
        return messages  # thinking blocks can't carry cache_control
    content[-1] = {**block, "cache_control": CACHE}
    last["content"] = content
    return [*messages[:-1], last]
