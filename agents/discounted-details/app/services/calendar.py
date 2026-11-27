"""Google Calendar access (PRD §9.3). `busy_blocks` merges the primary calendar's events (with locations) and
free/busy for any extra calendars. Transparent ("Free") events never block."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from app import clock
from app.services.sheets import UpstreamError


@dataclass
class Block:
    start: datetime
    end: datetime
    location: str | None = None
    event_id: str | None = None
    summary: str = ""
    private: dict[str, str] = field(default_factory=dict)


class Calendar(Protocol):
    async def busy_blocks(self, time_min: datetime, time_max: datetime) -> list[Block]: ...
    async def create_event(self, start: datetime, end: datetime, summary: str, location: str, description: str,
                           color_id: str, private: dict[str, str]) -> str: ...
    async def patch_event(self, event_id: str, **fields: Any) -> None: ...
    async def delete_event(self, event_id: str) -> None: ...
    async def list_dd_events(self, time_min: datetime, time_max: datetime) -> list[Block]: ...


def _parse_event_time(t: dict[str, str], end: bool = False) -> datetime:
    if "dateTime" in t:
        return clock.parse_iso(t["dateTime"])
    d = date.fromisoformat(t["date"])
    return datetime.combine(d, time.min, tzinfo=clock.TZ)


def event_to_block(ev: dict[str, Any]) -> Block | None:
    if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
        return None
    return Block(start=_parse_event_time(ev["start"]), end=_parse_event_time(ev["end"], end=True),
                 location=ev.get("location") or None, event_id=ev.get("id"), summary=ev.get("summary", ""),
                 private=(ev.get("extendedProperties") or {}).get("private", {}))


class GoogleCalendar:
    def __init__(self, service_account_json: str, calendar_id: str, extra_calendar_ids: list[str] | None = None):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            json.loads(service_account_json), scopes=["https://www.googleapis.com/auth/calendar"])
        self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.calendar_id = calendar_id
        self.extra = extra_calendar_ids or []

    async def _call(self, req):
        last = None
        for attempt in range(3):
            try:
                return await asyncio.to_thread(req.execute)
            except Exception as e:  # googleapiclient.errors.HttpError / transport
                status = getattr(getattr(e, "resp", None), "status", None)
                last = e
                if status is not None and int(status) < 500 and int(status) != 429:
                    break
                await asyncio.sleep(2 ** attempt)
        raise UpstreamError(f"Google Calendar: {last}")

    async def _events(self, time_min: datetime, time_max: datetime, **extra) -> list[dict[str, Any]]:
        out, token = [], None
        while True:
            res = await self._call(self._svc.events().list(
                calendarId=self.calendar_id, timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
                singleEvents=True, orderBy="startTime", pageToken=token, maxResults=250, **extra))
            out += res.get("items", [])
            token = res.get("nextPageToken")
            if not token:
                return out

    async def busy_blocks(self, time_min: datetime, time_max: datetime) -> list[Block]:
        blocks = [b for ev in await self._events(time_min, time_max) if (b := event_to_block(ev))]
        if self.extra:
            fb = await self._call(self._svc.freebusy().query(body={
                "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(), "timeZone": "America/Chicago",
                "items": [{"id": c} for c in self.extra]}))
            for cal in fb.get("calendars", {}).values():
                for b in cal.get("busy", []):
                    blocks.append(Block(start=clock.parse_iso(b["start"]), end=clock.parse_iso(b["end"])))
        return sorted(blocks, key=lambda b: b.start)

    async def list_dd_events(self, time_min: datetime, time_max: datetime) -> list[Block]:
        evs = await self._events(time_min, time_max)
        return [b for ev in evs if (b := event_to_block(ev)) and b.private.get("dd_job_id")]

    async def create_event(self, start, end, summary, location, description, color_id, private) -> str:
        body = {"summary": summary, "location": location, "description": description, "colorId": color_id,
                "start": {"dateTime": start.isoformat(), "timeZone": "America/Chicago"},
                "end": {"dateTime": end.isoformat(), "timeZone": "America/Chicago"},
                "extendedProperties": {"private": private}}
        ev = await self._call(self._svc.events().insert(calendarId=self.calendar_id, body=body, sendUpdates="none"))
        return ev["id"]

    async def patch_event(self, event_id: str, **fields: Any) -> None:
        body: dict[str, Any] = {}
        for k in ("start", "end"):
            if k in fields:
                body[k] = {"dateTime": fields.pop(k).isoformat(), "timeZone": "America/Chicago"}
        body.update(fields)
        await self._call(self._svc.events().patch(calendarId=self.calendar_id, eventId=event_id, body=body,
                                                  sendUpdates="none"))

    async def delete_event(self, event_id: str) -> None:
        await self._call(self._svc.events().delete(calendarId=self.calendar_id, eventId=event_id, sendUpdates="none"))


class FakeCalendar:
    """In-memory calendar for tests."""

    def __init__(self):
        self.events: dict[str, Block] = {}
        self._n = 0
        self.fail = False

    def add(self, start: datetime, end: datetime, location: str | None = None, summary: str = "") -> str:
        self._n += 1
        eid = f"ev{self._n}"
        self.events[eid] = Block(start, end, location, eid, summary)
        return eid

    async def busy_blocks(self, time_min, time_max):
        if self.fail:
            raise UpstreamError("calendar down")
        return sorted([b for b in self.events.values() if b.end > time_min and b.start < time_max],
                      key=lambda b: b.start)

    async def list_dd_events(self, time_min, time_max):
        return [b for b in await self.busy_blocks(time_min, time_max) if b.private.get("dd_job_id")]

    async def create_event(self, start, end, summary, location, description, color_id, private):
        if self.fail:
            raise UpstreamError("calendar down")
        eid = self.add(start, end, location, summary)
        self.events[eid].private = dict(private)
        return eid

    async def patch_event(self, event_id, **fields):
        b = self.events[event_id]
        for k, v in fields.items():
            setattr(b, k if k != "summary" else "summary", v)

    async def delete_event(self, event_id):
        self.events.pop(event_id, None)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=clock.TZ)
    return start, start + timedelta(days=1)
