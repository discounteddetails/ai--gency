"""FakeWorld: in-memory stand-ins for Composio, Maps and the Anthropic client (PRD Step 19)."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any

from app import clock
from app.core import Dex, DexConfig
from app.db import DB
from app.jobs.dm_poller import DMPoller
from app.jobs.tasks import Jobs
from app.seed import SERVICES_ROWS, SETTINGS_ROWS
from app.services.calendar import FakeCalendar
from app.services.dm_client import DMClient
from app.services.maps import Drive
from app.services.owner_telegram import FakeOwner
from app.services.sheets import MemoryStore, SheetsCRM
from app.webhooks.telegram import OwnerBot

PAGE_ID, IG_ID = "945217095333956", "29680530378297751"
_ids = itertools.count(1)


def _stamp() -> str:
    """Unique, increasing ISO timestamp (like Graph updated_time)."""
    from datetime import timedelta
    return (clock.now() + timedelta(microseconds=next(_ids))).isoformat()


class FakeMaps:
    def __init__(self, miles: float = 10, minutes: float = 20):
        self.miles, self.minutes = miles, minutes

    async def geocode(self, address):
        return address

    async def drive(self, origin, destination):
        return Drive(self.miles, self.minutes)


class FakeComposio:
    def __init__(self):
        self.convs: dict[str, dict[str, dict[str, Any]]] = {"instagram": {}, "messenger": {}}
        self.sent: list[dict[str, Any]] = []
        self.fail_send = False

    def _business(self, ch):
        return {"id": IG_ID, "username": "discounteddetails"} if ch == "instagram" else \
            {"id": PAGE_ID, "name": "Discounted Details"}

    def customer_says(self, ch: str, user_id: str, text: str, name: str = "Maria G", at=None) -> str:
        conv = self.convs[ch].setdefault(f"c_{user_id}", {"participants": [
            {"id": user_id, "username" if ch == "instagram" else "name": name}, self._business(ch)], "messages": []})
        mid = f"m_{next(_ids)}"
        when = at or clock.now()
        conv["messages"].append({"id": mid, "created_time": when.isoformat(), "message": text,
                                 "from": {"id": user_id}})
        conv["updated_time"] = _stamp()
        return mid

    def owner_says(self, ch: str, user_id: str, text: str) -> str:
        conv = self.convs[ch][f"c_{user_id}"]
        mid = f"m_{next(_ids)}"
        conv["messages"].append({"id": mid, "created_time": clock.now().isoformat(), "message": text,
                                 "from": self._business(ch)})
        conv["updated_time"] = _stamp()
        return mid

    async def execute(self, slug: str, args: dict[str, Any]) -> dict[str, Any]:
        ch = "instagram" if slug.startswith("INSTAGRAM") else "messenger"
        if slug in ("INSTAGRAM_LIST_ALL_CONVERSATIONS", "FACEBOOK_GET_PAGE_CONVERSATIONS"):
            return {"data": [{"id": cid, "updated_time": c.get("updated_time", ""),
                              "participants": {"data": c["participants"]}} for cid, c in self.convs[ch].items()]}
        if slug in ("INSTAGRAM_LIST_ALL_MESSAGES", "FACEBOOK_GET_CONVERSATION_MESSAGES"):
            msgs = self.convs[ch][args["conversation_id"]]["messages"]
            return {"data": list(reversed(msgs))[: args.get("limit", 10)]}
        if slug in ("INSTAGRAM_SEND_TEXT_MESSAGE", "FACEBOOK_SEND_MESSAGE"):
            if self.fail_send:
                from app.services.composio_client import WindowClosedError
                raise WindowClosedError("outside of allowed window")
            rid = args["recipient_id"]
            text = args.get("text") or args.get("message_text")
            mid = self.owner_says(ch, rid, text) if f"c_{rid}" in self.convs[ch] else f"m_{next(_ids)}"
            self.sent.append({"channel": ch, "to": rid, "text": text, "tag": args.get("tag"), "mid": mid})
            return {"recipient_id": rid, "message_id": mid}
        raise AssertionError(f"unexpected tool {slug}")

    async def proxy(self, toolkit, endpoint, method, body):
        rid = body["recipient"]["id"]
        mid = self.owner_says("instagram", rid, body["message"]["text"])
        self.sent.append({"channel": "instagram", "to": rid, "text": body["message"]["text"], "tag": body.get("tag"),
                          "mid": mid})
        return {"message_id": mid}


# ------------------------------------------------------------------ scripted Claude
@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, **_):
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class Resp:
    content: list[Block]
    stop_reason: str
    usage: Any = None


def tool(name: str, **inp) -> Block:
    return Block(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=inp)


def say(text: str) -> Block:
    return Block(type="text", text=text)


class ScriptedClaude:
    """Each step is either a list of blocks (tool_use -> stop 'tool_use', else 'end_turn') or a callable(messages)."""

    def __init__(self):
        self.steps: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def queue(self, *steps):
        self.steps.extend(steps)

    async def create(self, **kw):
        import copy
        kw = {**kw, "messages": copy.deepcopy(kw["messages"])}  # snapshot: the runner keeps appending
        self.calls.append(kw)
        if not self.steps:
            return Resp([say("ok")], "end_turn")
        step = self.steps.pop(0)
        if callable(step):
            step = step(kw["messages"])
        if isinstance(step, Resp):
            return step
        stop = "tool_use" if any(b.type == "tool_use" for b in step) else "end_turn"
        return Resp(step, stop)

    def last_tool_results(self) -> list[dict[str, Any]]:
        msgs = self.calls[-1]["messages"]
        return [json.loads(r["content"]) | {"is_error": r.get("is_error", False)} for r in msgs[-1]["content"]
                if r.get("type") == "tool_result"]


# ------------------------------------------------------------------ world
class World:
    def __init__(self, shadow: bool = False, settings_overrides: dict[str, Any] | None = None):
        store = MemoryStore()
        for r in SERVICES_ROWS:
            store.append("Services", r)
        overrides = settings_overrides or {}
        for k, v, d in SETTINGS_ROWS:
            store.append("Settings", {"key": k, "value": overrides.get(k, v), "description": d})
        self.store = store
        self.db = DB(":memory:")
        self.crm = SheetsCRM(store)
        self.calendar = FakeCalendar()
        self.maps = FakeMaps()
        self.composio = FakeComposio()
        self.owner = FakeOwner()
        self.dex = Dex(cfg=DexConfig(signing_key="k", debounce_seconds=0, shadow_default=shadow), db=self.db,
                       crm=self.crm, calendar=self.calendar, maps=self.maps,
                       dm=DMClient(self.composio, self.db, PAGE_ID), owner=self.owner)
        from app.agent.runner import AgentRunner

        self.claude = ScriptedClaude()
        self.runner = AgentRunner(self.dex, self.claude, "claude-opus-5-5", 10_000_000)
        self.poller = DMPoller(self.composio, self.db, page_id=PAGE_ID, ig_user_id=IG_ID,
                               on_customer_message=self.dex.on_customer_message,
                               on_owner_message=self.dex.on_owner_message)
        self.jobs = Jobs(self.dex)
        self.bot = OwnerBot(self.dex, 9229985118, "secret")

    async def start(self):
        await self.poller.poll_once()  # first run: record state only

    async def tick(self):
        n = await self.poller.poll_once()
        await self.dex.debouncer.drain()
        return n

    def rows(self, tab):
        return self.store.read_all(tab)

    def sent_texts(self):
        return [s["text"] for s in self.composio.sent]
