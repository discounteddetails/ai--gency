"""Which way can Dex reach this customer right now? (PRD §6.8)"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DM_WINDOW = timedelta(hours=24) - timedelta(minutes=15)
HUMAN_AGENT_WINDOW = timedelta(days=7) - timedelta(minutes=30)


def dm_window_state(last_inbound_at: datetime | None, now: datetime) -> tuple[str, float]:
    """('open', minutes_left) | ('human_agent', hours_left) | ('closed', 0)."""
    if not last_inbound_at:
        return "closed", 0
    age = now - last_inbound_at
    if age < DM_WINDOW:
        return "open", (DM_WINDOW - age).total_seconds() / 60
    if age < HUMAN_AGENT_WINDOW:
        return "human_agent", (HUMAN_AGENT_WINDOW - age).total_seconds() / 3600
    return "closed", 0


def human_agent_enabled(channel: str, settings: dict[str, Any]) -> bool:
    return bool(settings.get(f"human_agent_{channel}", False))


def choose_channel(channel: str, last_inbound_at: datetime | None, kind: str, now: datetime,
                   settings: dict[str, Any]) -> str:
    """kind: 'reply' (answering the customer) or 'proactive'. Returns 'dm' | 'human_agent' | 'owner'."""
    state, _ = dm_window_state(last_inbound_at, now)
    if state == "open":
        return "dm"
    if state == "human_agent" and kind == "reply" and human_agent_enabled(channel, settings):
        return "human_agent"
    return "owner"
