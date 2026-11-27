"""Single source of "now" so tests can freeze time. All business times are America/Chicago."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")


def now() -> datetime:
    return datetime.now(TZ)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def parse_iso(value: str) -> datetime:
    """Parse ISO-8601 (also Meta's '+0000' offsets) into an aware local datetime."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    if len(v) > 5 and v[-5] in "+-" and v[-3] != ":":  # +0000 -> +00:00
        v = v[:-2] + ":" + v[-2:]
    return to_local(datetime.fromisoformat(v))


def label(dt: datetime) -> str:
    """'Tue, Dec 2 at 9:00 AM' in local time."""
    d = to_local(dt)
    hour = d.strftime("%I:%M %p").lstrip("0")
    return f"{d.strftime('%a')}, {d.strftime('%b')} {d.day} at {hour}"
