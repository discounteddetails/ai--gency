from datetime import date, datetime, timedelta

import pytest

from app import clock
from app.services.calendar import FakeCalendar
from app.services.maps import Drive
from app.services.scheduling import SlotError, find_slots, recheck_slot, sign_slot, verify_slot

KEY = "test-key"
ADDR = "412 Elm St, Kyle, TX"


class FakeMaps:
    def __init__(self, minutes=20, fail=False):
        self.minutes, self.fail = minutes, fail

    async def geocode(self, address):
        return address

    async def drive(self, origin, destination):
        if self.fail:
            raise RuntimeError("routes down")
        return Drive(miles=10, minutes=self.minutes)


def T(day, h, m=0):
    return datetime(2026, 12, day, h, m, tzinfo=clock.TZ)


NOW = T(1, 6)  # Tue Dec 1, 6:00 AM


async def slots(cal, settings, dur=240, **kw):
    return await find_slots(calendar=cal, maps=FakeMaps(), settings=settings, signing_key=KEY,
                            duration_min=dur, address=ADDR, now=kw.pop("now", NOW), **kw)


def overlaps(slot, cal, settings, travel=20):
    """Spec check: drive+buffer before the job; after it, drive+buffer (or 30 min if the next event has no location)."""
    buf = settings["buffer_minutes"]
    for b in cal.events.values():
        after = travel + buf if b.location else 30
        if b.start < slot.end + timedelta(minutes=after) and b.end > slot.start - timedelta(minutes=travel + buf):
            return True
    return False


async def test_respects_notice_hours_and_end_of_day(settings):
    cal = FakeCalendar()
    res = await slots(cal, settings, max_results=5, earliest_date=date(2026, 12, 1), latest_date=date(2026, 12, 1))
    assert res and all(s.start >= NOW + timedelta(hours=3) for s in res)
    assert all(s.end <= T(1, 20) for s in res)
    assert res[0].start == T(1, 9)


async def test_four_hour_job_cannot_start_after_4pm(settings):
    cal = FakeCalendar()
    cal.add(T(2, 7), T(2, 15, 30))  # busy most of the day
    res = await slots(cal, settings, earliest_date=date(2026, 12, 2), latest_date=date(2026, 12, 2))
    assert res == []


async def test_no_overlap_with_buffers(settings):
    cal = FakeCalendar()
    cal.add(T(2, 9), T(2, 11), location="Buda, TX")
    cal.add(T(2, 14), T(2, 15))  # no location -> 30 min buffer
    res = await slots(cal, settings, dur=90, max_results=5, earliest_date=date(2026, 12, 2),
                      latest_date=date(2026, 12, 2))
    assert res
    for s in res:
        assert not overlaps(s, cal, settings)


async def test_all_day_busy_blocks_day(settings):
    cal = FakeCalendar()
    cal.add(T(3, 0), T(4, 0), summary="Day off")
    res = await slots(cal, settings, earliest_date=date(2026, 12, 3), latest_date=date(2026, 12, 3))
    assert res == []


async def test_horizon_limit(settings):
    res = await slots(FakeCalendar(), settings, max_results=5, earliest_date=date(2026, 12, 30))
    assert res == []


async def test_spread_across_days_and_max_two_per_day(settings):
    res = await slots(FakeCalendar(), settings, dur=90, max_results=3)
    days = [s.start.date() for s in res]
    assert len(set(days)) == 3


async def test_time_of_day_filter(settings):
    res = await slots(FakeCalendar(), settings, dur=90, time_of_day="afternoon")
    assert all(12 <= s.start.hour < 16 for s in res)


async def test_routes_failure_uses_fallback(settings):
    cal = FakeCalendar()
    cal.add(T(2, 9), T(2, 10), location="Austin, TX")
    res = await find_slots(calendar=cal, maps=FakeMaps(fail=True), settings=settings, signing_key=KEY,
                           duration_min=90, address=ADDR, now=NOW, earliest_date=date(2026, 12, 2),
                           latest_date=date(2026, 12, 2), max_results=5)
    assert all(s.start >= T(2, 11) or s.end <= T(2, 9) - timedelta(minutes=30) for s in res)


def test_slot_id_roundtrip_and_tamper():
    sid = sign_slot(KEY, T(2, 9), 240, ADDR, NOW)
    start, dur = verify_slot(KEY, sid, ADDR, NOW + timedelta(minutes=10))
    assert start == T(2, 9) and dur == 240
    with pytest.raises(SlotError) as e:
        verify_slot(KEY, sid, ADDR, NOW + timedelta(minutes=31))
    assert e.value.code == "SLOT_EXPIRED"
    with pytest.raises(SlotError):
        verify_slot("other-key", sid, ADDR, NOW)
    with pytest.raises(SlotError):
        verify_slot(KEY, sid, "different address", NOW)


async def test_recheck_detects_new_conflict(settings):
    cal = FakeCalendar()
    assert await recheck_slot(calendar=cal, maps=FakeMaps(), settings=settings, start=T(2, 9), duration_min=240,
                              address=ADDR)
    cal.add(T(2, 12), T(2, 13))
    assert not await recheck_slot(calendar=cal, maps=FakeMaps(), settings=settings, start=T(2, 9),
                                  duration_min=240, address=ADDR)
