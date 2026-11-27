"""Availability and booking (PRD §6.3). The calendar is re-checked under a lock at booking time, so a slot
offered earlier can never be double-booked."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from app import clock
from app.services.calendar import Block, Calendar
from app.services.maps import Maps

log = logging.getLogger(__name__)

FALLBACK_TRAVEL_MIN = 45
UNKNOWN_NEXT_LOCATION_MIN = 30
TIME_OF_DAY = {"morning": (time(7), time(11, 59)), "afternoon": (time(12), time(15, 59)),
               "evening": (time(16), time(23, 59)), "any": (time(0), time(23, 59))}

BOOKING_LOCK = asyncio.Lock()


class SlotError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class Slot:
    start: datetime
    end: datetime
    slot_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"slot_id": self.slot_id, "start_iso": self.start.isoformat(), "end_iso": self.end.isoformat(),
                "label": clock.label(self.start)}


# ---------- signed slot ids ----------
def _addr_hash(address: str | None) -> str:
    return hashlib.sha256((address or "").strip().lower().encode()).hexdigest()[:12]


def sign_slot(key: str, start: datetime, duration_min: int, address: str, now: datetime) -> str:
    expires = int((now + timedelta(minutes=30)).timestamp())
    payload = f"{start.isoformat()}|{duration_min}|{_addr_hash(address)}|{expires}"
    sig = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")


def verify_slot(key: str, slot_id: str, address: str, now: datetime) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(slot_id + "=" * (-len(slot_id) % 4)).decode()
        start_s, dur_s, ahash, exp_s, sig = raw.split("|")
    except Exception as e:
        raise SlotError("SLOT_INVALID", "Unreadable slot id") from e
    payload = f"{start_s}|{dur_s}|{ahash}|{exp_s}"
    good = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(good, sig):
        raise SlotError("SLOT_INVALID", "Signature mismatch")
    if now.timestamp() > int(exp_s):
        raise SlotError("SLOT_EXPIRED", "Offer older than 30 minutes; find slots again")
    if ahash != _addr_hash(address):
        raise SlotError("SLOT_INVALID", "Address differs from the one the slot was offered for")
    return clock.parse_iso(start_s), int(dur_s)


# ---------- availability ----------
def _hm(v: Any, default: str) -> time:
    s = str(v or default).strip().lstrip("'")
    h, m = s.split(":")[:2]
    return time(int(h), int(m))


async def _travel(maps: Maps, origin: str, dest: str, cache: dict) -> float:
    k = (origin, dest)
    if k not in cache:
        try:
            cache[k] = (await maps.drive(origin, dest)).minutes
        except Exception as e:  # Routes failure -> conservative fallback (FR-S3)
            log.warning("drive time fallback %s -> %s: %s", origin, dest, e)
            cache[k] = FALLBACK_TRAVEL_MIN
    return cache[k]


async def slot_conflicts(start: datetime, end: datetime, address: str, blocks: list[Block], maps: Maps,
                         settings: dict[str, Any], cache: dict) -> bool:
    """True if [start - travel_in, end + travel_out] intersects any busy block (FR-S2/S3)."""
    buffer = int(settings.get("buffer_minutes", 15))
    home = str(settings.get("home_base_address", "Kyle, TX 78640"))
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    same_day = [b for b in blocks if b.end > day_start and b.start < day_start + timedelta(days=1)]
    if any(b.start < end and b.end > start for b in same_day):
        return True
    prev = max((b for b in same_day if b.end <= start), key=lambda b: b.end, default=None)
    nxt = min((b for b in same_day if b.start >= end), key=lambda b: b.start, default=None)
    origin = prev.location if prev and prev.location else home
    travel_in = await _travel(maps, origin, address, cache) + buffer
    if prev and prev.end > start - timedelta(minutes=travel_in):
        return True
    if nxt:
        travel_out = (await _travel(maps, address, nxt.location, cache) + buffer) if nxt.location \
            else UNKNOWN_NEXT_LOCATION_MIN
        if nxt.start < end + timedelta(minutes=travel_out):
            return True
    return False


async def find_slots(*, calendar: Calendar, maps: Maps, settings: dict[str, Any], signing_key: str,
                     duration_min: int, address: str, now: datetime, earliest_date: date | None = None,
                     latest_date: date | None = None, time_of_day: str = "any", max_results: int = 3) -> list[Slot]:
    grid = int(settings.get("slot_grid_minutes", 30))
    w_start, w_end = _hm(settings.get("work_start"), "07:00"), _hm(settings.get("work_end"), "20:00")
    min_start = now + timedelta(hours=float(settings.get("min_notice_hours", 3)))
    horizon = now + timedelta(days=int(settings.get("booking_horizon_days", 21)))
    tod_lo, tod_hi = TIME_OF_DAY.get(time_of_day, TIME_OF_DAY["any"])

    first_day = max(now.date(), earliest_date or now.date())
    last_day = min(horizon.date(), latest_date or horizon.date())
    if first_day > last_day:
        return []
    blocks = await calendar.busy_blocks(datetime.combine(first_day, time.min, tzinfo=clock.TZ),
                                        datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=clock.TZ))
    cache: dict = {}
    per_day: dict[date, list[datetime]] = {}
    d = first_day
    while d <= last_day:
        t = datetime.combine(d, w_start, tzinfo=clock.TZ)
        day_close = datetime.combine(d, w_end, tzinfo=clock.TZ)
        while t + timedelta(minutes=duration_min) <= day_close:
            if t >= min_start and t <= horizon and tod_lo <= t.time() <= tod_hi:
                end = t + timedelta(minutes=duration_min)
                if not await slot_conflicts(t, end, address, blocks, maps, settings, cache):
                    picks = per_day.setdefault(d, [])
                    # at most 2 per day, and spread them out (3h apart)
                    if not picks or (len(picks) < 2 and t >= picks[0] + timedelta(hours=3)):
                        picks.append(t)
                    if len(picks) == 2:
                        break
            t += timedelta(minutes=grid)
        d += timedelta(days=1)

    days = sorted(per_day)
    chosen: list[datetime] = [per_day[x][0] for x in days][:max_results]
    if len(chosen) < max_results:
        chosen += [per_day[x][1] for x in days if len(per_day[x]) > 1][: max_results - len(chosen)]
    chosen.sort()
    return [Slot(s, s + timedelta(minutes=duration_min), sign_slot(signing_key, s, duration_min, address, now))
            for s in chosen]


async def recheck_slot(*, calendar: Calendar, maps: Maps, settings: dict[str, Any], start: datetime,
                       duration_min: int, address: str, ignore_event_ids: set[str] | None = None) -> bool:
    """True if the slot is still free (used at booking and when moving a job)."""
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    blocks = [b for b in await calendar.busy_blocks(day_start, day_start + timedelta(days=1))
              if not ignore_event_ids or b.event_id not in ignore_event_ids]
    return not await slot_conflicts(start, start + timedelta(minutes=duration_min), address, blocks, maps,
                                    settings, {})
