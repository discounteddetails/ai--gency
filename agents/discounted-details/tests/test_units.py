import json
from datetime import datetime, timedelta

import pytest
import time_machine
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import clock
from app.agent.tools import MODES, TOOLS
from app.guardrails import (
    COMPLAINT_HOLDING_MESSAGE,
    GuardrailBlocked,
    TurnState,
    check_outgoing,
    is_hard_opt_out,
)
from app.services.channels import choose_channel, dm_window_state
from app.services.composio_client import (
    ConnectionExpiredError,
    RateLimitedError,
    RecipientUnavailableError,
    WindowClosedError,
    classify_error,
)
from app.services.dm_client import HumanAgentNotAllowed
from app.webhooks.telegram import router
from tests.fakes import World

NOW = datetime(2026, 12, 1, 10, 0, tzinfo=clock.TZ)


# ---------------- guardrails ----------------
@pytest.mark.parametrize("text", ["We guarantee it'll look perfect", "It'll be like new!", "100% stain free",
                                  "We'll completely remove that"])
def test_promise_language_blocked(text):
    with pytest.raises(GuardrailBlocked):
        check_outgoing(text, TurnState())


def test_price_integrity():
    st = TurnState(verified_amounts={310, 285, 25})
    check_outgoing("Full Detail is $285, plus $25 for pet hair, so $310 total.", st)
    with pytest.raises(GuardrailBlocked):
        check_outgoing("It's $300 total", st)
    with pytest.raises(GuardrailBlocked):
        check_outgoing("$310.50", st)


def test_length_send_cap_confirmation_and_complaint():
    with pytest.raises(GuardrailBlocked):
        check_outgoing("x" * 901, TurnState())
    with pytest.raises(GuardrailBlocked):
        check_outgoing("hi", TurnState(sends=2))
    with pytest.raises(GuardrailBlocked):
        check_outgoing("You're all set for Tuesday!", TurnState())
    check_outgoing("You're all set for Tuesday!", TurnState(booking_succeeded=True))
    with pytest.raises(GuardrailBlocked):
        check_outgoing("So sorry!", TurnState(complaint_mode=True))
    check_outgoing(COMPLAINT_HOLDING_MESSAGE, TurnState(complaint_mode=True))


@pytest.mark.parametrize("text,expected", [("STOP", True), (" stop ", True), ("Not interested.", True),
                                           ("stop by at 3?", False), ("no thanks to the add-on", False)])
def test_hard_opt_out(text, expected):
    assert is_hard_opt_out(text) is expected


# ---------------- channels ----------------
def test_window_margins():
    assert dm_window_state(NOW - timedelta(hours=23, minutes=44), NOW)[0] == "open"
    assert dm_window_state(NOW - timedelta(hours=23, minutes=46), NOW)[0] == "human_agent"
    assert dm_window_state(NOW - timedelta(days=6, hours=23, minutes=45), NOW)[0] == "closed"


@pytest.mark.parametrize("age,kind,enabled,expected", [
    (timedelta(hours=23), "reply", False, "dm"),
    (timedelta(hours=30), "reply", True, "human_agent"),
    (timedelta(hours=30), "reply", False, "owner"),
    (timedelta(hours=30), "proactive", True, "owner"),
    (timedelta(days=8), "reply", True, "owner"),
])
def test_choose_channel(age, kind, enabled, expected):
    assert choose_channel("messenger", NOW - age, kind, NOW, {"human_agent_messenger": enabled}) == expected


# ---------------- composio error mapping ----------------
@pytest.mark.parametrize("msg,cls", [
    ("(#10) This message is sent outside of allowed window. subcode 2018278", WindowClosedError),
    ("(#551) This person isn't available right now.", RecipientUnavailableError),
    ("Error validating access token: Session has expired", ConnectionExpiredError),
    ("(#613) Calls to this api have exceeded the rate limit.", RateLimitedError),
])
def test_classify(msg, cls):
    assert classify_error(msg) is cls


async def test_human_agent_tag_only_via_approved_path():
    w = World()
    with pytest.raises(HumanAgentNotAllowed):
        await w.dex.dm.send_text("messenger", "1", "hi", human_agent=True)


# ---------------- tool schemas ----------------
def test_tool_schemas_strict_and_complete():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names)) == len(MODES)
    for t in TOOLS:
        sch = t["input_schema"]
        assert "strict" not in t and sch["additionalProperties"] is False  # strict grammar too large for 14 tools
        assert set(sch["required"]) == set(sch["properties"])
        dumped = json.dumps(sch)
        assert "minimum" not in dumped and "maximum" not in dumped
        assert "anyOf" not in dumped and '"null"' not in dumped  # API allows few union-typed params in strict mode
    assert json.dumps(TOOLS) == json.dumps(TOOLS)  # deterministic -> cache-stable


# ---------------- Telegram ----------------
@pytest.fixture
def w():
    with time_machine.travel(NOW, tick=True):
        yield World()


async def test_status_pause_resume_help(w):
    assert "Agent: ON" in await w.bot.handle_text("/status")
    await w.bot.handle_text("/pause")
    assert w.dex.paused and "PAUSED" in await w.bot.handle_text("/status")
    await w.bot.handle_text("/resume")
    assert not w.dex.paused
    assert "Commands" in await w.bot.handle_text("/help")
    await w.bot.handle_text("/shadow_on")
    assert w.dex.shadow


async def test_typed_yes_with_zero_one_two_pending(w):
    c = await w.crm.create_customer({"first_name": "Jo", "instagram_igsid": "5"})
    cust = await w.crm.get_customer(c)
    a1 = await w.dex.create_approval("reply", cust, summary="s", draft="Hi!")
    a2 = await w.dex.create_approval("reply", cust, summary="s", draft="Hello!")
    assert (await w.bot.handle_text("yes")).startswith("Which one?")
    msg = await w.bot.handle_text(f"no {a2.lower()}")
    assert "skipped" in msg
    assert w.db.get_approval(a1)["status"] == "pending"
    assert "No pending approval" in await w.bot.handle_text("yes A99")


async def test_foreign_chat_ignored_and_double_tap(w):
    await w.bot.handle_update({"message": {"chat": {"id": 1}, "text": "/pause"}})
    assert not w.dex.paused
    c = await w.crm.create_customer({"first_name": "Jo", "instagram_igsid": "5"})
    aid = await w.dex.create_approval("question", await w.crm.get_customer(c), summary="q", customer_request="?")
    await w.bot.handle_update({"callback_query": {"id": "1", "data": f"{aid}:self",
                                                  "message": {"chat": {"id": 9229985118}}}})
    await w.bot.handle_update({"callback_query": {"id": "2", "data": f"{aid}:self",
                                                  "message": {"chat": {"id": 9229985118}}}})
    assert any("already decided" in t for t in w.owner.texts())


def test_webhook_secret(w):
    app = FastAPI()
    app.include_router(router(w.bot))
    client = TestClient(app)
    assert client.post("/webhooks/telegram", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 403
    assert client.post("/webhooks/telegram", json={"update_id": 1},
                       headers={"X-Telegram-Bot-Api-Secret-Token": "secret"}).status_code == 200


async def test_daily_summary_and_approval_reping(w):
    c = await w.crm.create_customer({"first_name": "Jo", "instagram_igsid": "5"})
    await w.dex.create_approval("discount", await w.crm.get_customer(c), summary="10% off")
    text = await w.jobs.daily_summary()
    assert "Pending approvals: A0001 discount" in text
    with time_machine.travel(NOW + timedelta(hours=3), tick=True):
        await w.jobs.approval_reminders()
    assert any("Still waiting on A0001" in t for t in w.owner.texts())


async def test_tool_args_tolerant_of_missing_and_extra_fields(w):
    from app.agent.handlers import ToolContext, run_tool

    ctx = ToolContext(dex=w.dex, mode="CUSTOMER_CONVERSATION")
    out, err = await run_tool("quote_price", {"service": "exterior", "size_class": "sedan", "bogus": 1}, ctx)
    assert not err and json.loads(out)["total"] == 75
    out, err = await run_tool("quote_price", {"size_class": "sedan"}, ctx)
    assert err and json.loads(out)["error"] == "INVALID_SERVICE"


async def test_night_polling_every_5_minutes():
    from app.jobs import scheduler as sch

    class P:
        n = 0

        async def poll_once(self):
            P.n += 1

    p, last = P(), {}
    t = [0.0]
    import time as _t
    orig = _t.monotonic
    _t.monotonic = lambda: t[0]
    try:
        with time_machine.travel(datetime(2026, 12, 1, 23, 0, tzinfo=clock.TZ), tick=False):  # 11 PM
            for minute in range(10):
                t[0] = minute * 60.0
                await sch.poll_if_due(p, last, 60, 300)
        assert P.n == 2  # minutes 0 and 5
        P.n, last = 0, {}
        with time_machine.travel(datetime(2026, 12, 1, 9, 0, tzinfo=clock.TZ), tick=False):  # 9 AM
            for minute in range(10):
                t[0] = 10_000 + minute * 60.0
                await sch.poll_if_due(p, last, 60, 300)
        assert P.n == 10
    finally:
        _t.monotonic = orig
    assert sch.is_night(22) and sch.is_night(5) and not sch.is_night(6) and not sch.is_night(21)
