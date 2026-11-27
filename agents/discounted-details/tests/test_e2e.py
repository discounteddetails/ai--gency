"""Integration scenarios (PRD §11.2) through the poller, agent loop, CRM, calendar and Telegram, using fakes."""

import json
from datetime import datetime, timedelta

import pytest
import time_machine

from app import clock
from app.guardrails import COMPLAINT_HOLDING_MESSAGE, DNC_CLOSING_MESSAGE
from tests.fakes import World, say, tool

NOW = datetime(2026, 12, 1, 8, 0, tzinfo=clock.TZ)  # Tue 8:00 AM
ADDR = "412 Elm St, Kyle, TX 78640"


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=True):
        yield


def results(messages):
    return [json.loads(r["content"]) for r in messages[-1]["content"] if r.get("type") == "tool_result"]


def first_slot(messages):
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            for r in m["content"]:
                if r.get("type") == "tool_result" and "slots" in r["content"]:
                    return json.loads(r["content"])["slots"][0]
    raise AssertionError("no slots in history")


async def booking_conversation(w: World):
    q = dict(service="full", size_class="large_suv", addons=["pet_hair"], condition="standard")
    w.claude.queue(
        [tool("quote_price", **q, address=ADDR)],
        [tool("find_available_slots", **q, address=ADDR, earliest_date=None, latest_date=None, time_of_day="any",
              max_results=3)],
        lambda msgs: [tool("send_reply", customer_id=None,
                           text=f"A Full Detail for your Tahoe with pet hair removal is $310. I have "
                                f"{first_slot(msgs)['label']}. Does that work?")],
        [say("done")],
    )
    w.composio.customer_says("instagram", "111", "how much for a full detail on my 2020 tahoe? dog hair everywhere. "
                                                 "412 Elm St Kyle")
    await w.tick()
    w.claude.queue(
        lambda msgs: [tool("book_appointment", slot_id=first_slot(msgs)["slot_id"], customer_name="Mike R",
                           phone="512-555-0182", address=ADDR,
                           vehicle={"year": 2020, "make": "Chevy", "model": "Tahoe", "color": None,
                                    "size_class": "large_suv"}, service="full", addons=["pet_hair"],
                           condition="standard", notes=None)],
        lambda msgs: [tool("send_reply", customer_id=None,
                           text=f"You're all set! ✅ Full Detail + pet hair, {results(msgs)[0]['start_label']}. "
                                f"Total: ${results(msgs)[0]['total']}, paid after the job.")],
        [say("done")],
    )
    w.composio.customer_says("instagram", "111", "the first one works. Mike, 512-555-0182")
    await w.tick()


async def test_i1_new_lead_books_end_to_end(frozen):
    w = World()
    await w.start()
    await booking_conversation(w)
    assert len(w.composio.sent) == 2
    assert "$310" in w.sent_texts()[0] and "You're all set" in w.sent_texts()[1]
    jobs = w.rows("Jobs")
    assert len(jobs) == 1 and jobs[0]["total"] == 310 and jobs[0]["status"] == "Booked"
    ev = w.calendar.events[jobs[0]["calendar_event_id"]]
    assert ev.private["dd_job_id"] == jobs[0]["job_id"] and ev.location == ADDR
    assert (ev.end - ev.start) == timedelta(minutes=270)
    cust = w.rows("Customers")[0]
    assert cust["lead_status"] == "Booked" and cust["phone"] == "+15125550182"
    fus = w.rows("Follow-ups")
    assert fus[0]["type"] == "reminder" and fus[0]["sender"] == "agent"
    assert len([m for m in w.rows("Messages") if m["direction"] == "out"]) == 2
    # tools are strict and the system prompt is cached
    call = w.claude.calls[0]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["fallbacks"] == "default"


async def test_i2_slot_taken_between_offer_and_booking(frozen):
    w = World()
    await w.start()
    q = dict(service="exterior", size_class="sedan", addons=[], condition="standard")
    w.claude.queue([tool("find_available_slots", **q, address=ADDR, earliest_date=None, latest_date=None,
                         time_of_day="any", max_results=3)], [say("ok")])
    w.composio.customer_says("messenger", "222", "exterior detail on a civic?")
    await w.tick()
    slot = first_slot(w.claude.calls[-1]["messages"])
    start = clock.parse_iso(slot["start_iso"])
    w.calendar.add(start, start + timedelta(hours=1))  # owner fills the slot by hand
    w.claude.queue([tool("book_appointment", slot_id=slot["slot_id"], customer_name="Ana", phone=None, address=ADDR,
                         vehicle={"year": None, "make": "Honda", "model": "Civic", "color": None,
                                  "size_class": "sedan"}, service="exterior", addons=[], condition="standard",
                         notes=None)], [say("ok")])
    w.composio.customer_says("messenger", "222", "first one")
    await w.tick()
    res = w.claude.last_tool_results()
    assert res[0]["error"] == "SLOT_TAKEN"
    assert w.rows("Jobs") == []


async def test_guardrails_block_unverified_price_and_promises(frozen):
    w = World()
    await w.start()
    w.claude.queue([tool("send_reply", customer_id=None, text="It's only $99 and we guarantee perfect results!")],
                   [say("ok")])
    w.composio.customer_says("instagram", "333", "price?")
    await w.tick()
    res = w.claude.last_tool_results()[0]
    assert res["error"] == "GUARDRAIL_BLOCKED" and w.composio.sent == []


async def test_i6_complaint_only_holding_message(frozen):
    w = World()
    await w.start()
    w.claude.queue([tool("request_owner_approval", type="complaint", summary="Unhappy with interior",
                         customer_request="you missed spots", job_id=None, slot_id=None, address=None,
                         standard_total=None, draft_customer_message="I'm sorry! We'll come back to fix it.",
                         recipient_customer_ids=[])],
                   [tool("send_reply", customer_id=None, text="Sorry about that, we'll fix it for free!")],
                   [tool("send_reply", customer_id=None, text=COMPLAINT_HOLDING_MESSAGE)], [say("ok")])
    w.composio.customer_says("instagram", "444", "you missed spots, not happy")
    await w.tick()
    assert w.sent_texts() == [COMPLAINT_HOLDING_MESSAGE]
    assert any("COMPLAINT" in t for t in w.owner.texts("buttons"))


async def test_i7_hard_opt_out_and_dnc_customer_ignored(frozen):
    w = World()
    await w.start()
    w.composio.customer_says("instagram", "555", "STOP")
    await w.tick()
    assert w.sent_texts() == [DNC_CLOSING_MESSAGE] and w.claude.calls == []
    assert w.rows("Customers")[0]["lead_status"] == "DNC"
    w.composio.customer_says("instagram", "555", "actually what's the price")
    await w.tick()
    assert w.claude.calls == [] and any("Do-Not-Contact" in t for t in w.owner.texts())


async def test_i8_owner_manual_reply_pauses_thread(frozen):
    w = World()
    await w.start()
    w.claude.queue([say("ok")])
    w.composio.customer_says("messenger", "666", "hi")
    await w.tick()
    calls = len(w.claude.calls)
    w.composio.owner_says("messenger", "666", "Hey it's the owner, I'll handle this one")
    w.composio.customer_says("messenger", "666", "great thanks")
    await w.tick()
    assert len(w.claude.calls) == calls
    assert w.db.get_thread("messenger:666")["paused_until"] > clock.now() + timedelta(hours=11)
    assert any(m["direction"] == "owner_manual" for m in w.rows("Messages"))


async def test_dex_own_messages_not_mistaken_for_owner(frozen):
    w = World()
    await w.start()
    w.claude.queue([tool("send_reply", customer_id=None, text="Hi! What vehicle is it?")], [say("ok")])
    w.composio.customer_says("instagram", "777", "hello")
    await w.tick()
    await w.tick()
    assert not w.dex.is_paused(w.db.get_thread("instagram:777"))


async def test_first_poll_never_replies_to_old_messages(frozen):
    w = World()
    w.composio.customer_says("instagram", "888", "old message from last week")
    await w.start()
    await w.tick()
    assert w.claude.calls == []


async def test_shadow_mode_reply_needs_owner_tap(frozen):
    w = World(shadow=True)
    await w.start()
    w.claude.queue([tool("send_reply", customer_id=None, text="Hi! What vehicle is it?")], [say("ok")])
    w.composio.customer_says("instagram", "999", "hello")
    await w.tick()
    assert w.composio.sent == []
    aid = w.db.list_approvals("pending")[0]["approval_id"]
    await w.bot.handle_update({"callback_query": {"id": "cb1", "data": f"{aid}:approve",
                                                  "message": {"chat": {"id": 9229985118}}}})
    assert w.sent_texts() == ["Hi! What vehicle is it?"]


async def test_i3_discount_set_price_then_booking_uses_override(frozen):
    w = World()
    await w.start()
    w.claude.queue([tool("request_owner_approval", type="discount", summary="Wants 15% off Full Detail",
                         customer_request="any discount?", job_id=None, slot_id=None, address=None,
                         standard_total=310, draft_customer_message="", recipient_customer_ids=[])], [say("ok")])
    w.composio.customer_says("instagram", "121", "any discount?")
    await w.tick()
    aid = w.db.list_approvals("pending")[0]["approval_id"]
    w.claude.queue(lambda msgs: [tool("send_reply", customer_id=None,
                                      text="Good news! The owner approved $250 for your Full Detail.")], [say("ok")])
    await w.bot.handle_update({"callback_query": {"id": "c", "data": f"{aid}:price",
                                                  "message": {"chat": {"id": 9229985118}}}})
    await w.bot.handle_update({"message": {"chat": {"id": 9229985118}, "text": "250"}})
    assert w.sent_texts()[-1].startswith("Good news") and "$250" in w.sent_texts()[-1]
    assert w.db.get_approval(aid)["payload"]["override_price"] == 250


async def test_i4_reschedule_moves_event_and_releases_hold(frozen):
    w = World()
    await w.start()
    await booking_conversation(w)
    job = w.rows("Jobs")[0]
    q = dict(service="full", size_class="large_suv", addons=["pet_hair"], condition="standard")
    w.claude.queue(
        [tool("find_available_slots", **q, address=ADDR, earliest_date="2026-12-08", latest_date=None,
              time_of_day="any", max_results=3)],
        lambda msgs: [tool("request_owner_approval", type="reschedule", summary="Move Full Detail",
                           customer_request="can we do next week?", job_id=job["job_id"],
                           slot_id=first_slot(msgs)["slot_id"], address=ADDR, standard_total=None,
                           draft_customer_message="", recipient_customer_ids=[])],
        [tool("send_reply", customer_id=None, text="Got it! I've sent this to the owner to confirm.")], [say("ok")])
    w.composio.customer_says("instagram", "111", "can we move it to next week?")
    await w.tick()
    appr = w.db.list_approvals("pending")[0]
    hold = appr["payload"]["hold_event_id"]
    assert hold in w.calendar.events
    w.claude.queue([tool("send_reply", customer_id=None, text="You're all set for the new time!")], [say("ok")])
    await w.bot.handle_update({"callback_query": {"id": "c", "data": f"{appr['approval_id']}:approve",
                                                  "message": {"chat": {"id": 9229985118}}}})
    assert hold not in w.calendar.events
    moved = w.calendar.events[job["calendar_event_id"]]
    assert moved.start == clock.parse_iso(appr["payload"]["new_start"])
    assert w.sent_texts()[-1] == "You're all set for the new time!"


async def test_i13_late_reply_human_agent(frozen):
    w = World(settings_overrides={"human_agent_messenger": True})
    await w.start()
    w.claude.queue([tool("escalate_to_owner", reason="unknown_question", question="Do you do boats?",
                         urgency="normal")], [say("ok")])
    w.composio.customer_says("messenger", "131", "do you do boats?")
    await w.tick()
    aid = w.db.list_approvals("pending")[0]["approval_id"]
    with time_machine.travel(NOW + timedelta(days=2), tick=True):
        w.claude.queue([tool("send_reply", customer_id=None, text="Sorry, we only detail cars and trucks right now.")],
                       [say("ok")])
        await w.bot.handle_update({"callback_query": {"id": "c", "data": f"{aid}:answer",
                                                      "message": {"chat": {"id": 9229985118}}}})
        await w.bot.handle_update({"message": {"chat": {"id": 9229985118}, "text": "no boats, cars and trucks only"}})
        late = [a for a in w.db.list_approvals("pending") if a["type"] == "late_reply"]
        assert late and w.composio.sent == []
        await w.bot.handle_update({"callback_query": {"id": "c2", "data": f"{late[0]['approval_id']}:approve",
                                                      "message": {"chat": {"id": 9229985118}}}})
    assert w.composio.sent[-1]["tag"] == "HUMAN_AGENT"
    assert any(m["sent_via"] == "human_agent" for m in w.rows("Messages"))


async def test_late_reply_without_human_agent_goes_to_owner_list(frozen):
    w = World()
    await w.start()
    w.claude.queue([say("ok")])
    w.composio.customer_says("instagram", "141", "hi")
    await w.tick()
    customer = (await w.crm.list_customers())[0]
    with time_machine.travel(NOW + timedelta(days=2), tick=True):
        route = await w.dex.deliver_owner_text(customer, "Following up!")
    assert route == "owner" and w.rows("Follow-ups")[-1]["sender"] == "owner"


async def test_i10_job_lifecycle_and_followups(frozen):
    w = World()
    await w.start()
    await booking_conversation(w)
    job = w.rows("Jobs")[0]
    start = clock.parse_iso(str(job["start"]))
    # day before at 9:00: reminder is due, DM window long closed -> goes to owner's list with a ready message
    with time_machine.travel(start - timedelta(hours=start.hour) - timedelta(days=1) + timedelta(hours=9, minutes=1)):
        await w.jobs.send_due_agent_followups()
    rem = [f for f in w.rows("Follow-ups") if f["type"] == "reminder"][0]
    assert rem["sender"] in ("owner", "agent")
    assert "reminder" in rem["message"].lower() or rem["status"] == "Sent"
    with time_machine.travel(start + timedelta(hours=13)):
        assert await w.jobs.auto_complete_jobs() == [job["job_id"]]
        assert await w.jobs.post_job_followups() == 2
        assert await w.jobs.post_job_followups() == 0
    types = {f["type"] for f in w.rows("Follow-ups")}
    assert {"review_request", "rebook"} <= types
    assert w.rows("Customers")[0]["lead_status"] == "Customer"


async def test_i11_calendar_down(frozen):
    w = World()
    await w.start()
    w.calendar.fail = True
    w.claude.queue([tool("find_available_slots", service="exterior", size_class="sedan", addons=[],
                         condition="standard", address=ADDR, earliest_date=None, latest_date=None,
                         time_of_day="any", max_results=3)], [say("ok")])
    w.composio.customer_says("instagram", "151", "openings?")
    await w.tick()
    assert w.claude.last_tool_results()[0]["error"] == "UPSTREAM_ERROR"


async def test_i12_bulk_promo(frozen):
    w = World()
    await w.start()
    w.claude.queue([say("ok")], [say("ok")])
    w.composio.customer_says("instagram", "161", "hi")
    w.composio.customer_says("messenger", "162", "hello")
    await w.tick()
    cids = [c["customer_id"] for c in await w.crm.list_customers()]
    with time_machine.travel(NOW + timedelta(hours=30), tick=True):
        w.composio.customer_says("instagram", "161", "hey again")
        w.claude.queue([say("ok")])
        await w.tick()
        aid = await w.dex.create_approval("bulk_message", None, summary="Promo",
                                          draft="15% off interiors this week!", payload={"recipient_ids": cids})
        msg = await w.dex.decide(aid, "approve")
    assert "1 sent by DM, 1 added" in msg


async def test_mode_restrictions(frozen):
    w = World()
    await w.start()
    w.claude.queue([tool("reply_to_owner", text="hi")], [say("ok")])
    w.composio.customer_says("instagram", "171", "hi")
    await w.tick()
    assert w.claude.last_tool_results()[0]["error"] == "TOOL_NOT_ALLOWED_IN_MODE"


async def test_quote_followups_day_2_5_7_and_digest(frozen):
    w = World()
    await w.start()
    q = dict(service="interior", size_class="sedan", addons=[], condition="standard")
    w.claude.queue([tool("quote_price", **q, address=ADDR)],
                   [tool("send_reply", customer_id=None, text="An Interior Detail for your Camry is $165.")], [say("ok")])
    w.composio.customer_says("instagram", "181", "price for interior on my camry?", name="ktx.test")
    await w.tick()
    cust = (await w.crm.list_customers())[0]
    assert cust["lead_status"] == "Quoted"
    # still chatting after 3 hours: no sequence yet
    with time_machine.travel(NOW + timedelta(hours=3), tick=True):
        await w.jobs.quote_checkins()
    assert not [f for f in w.rows("Follow-ups") if f["type"] == "quote_follow_up"]
    # quiet for a day -> 3 owner follow-ups due on days 2, 5 and 7 at 10 AM, each worded differently
    with time_machine.travel(NOW + timedelta(hours=25), tick=True):
        await w.jobs.quote_checkins()
        await w.jobs.quote_checkins()  # idempotent
    seq = [f for f in w.rows("Follow-ups") if f["type"] == "quote_follow_up"]
    assert len(seq) == 3 and all(f["sender"] == "owner" for f in seq)
    assert [str(f["due_at"])[:10] for f in seq] == ["2026-12-03", "2026-12-06", "2026-12-08"]
    assert len({f["message"] for f in seq}) == 3 and "$165" in seq[0]["message"]
    # day 2, 10 AM digest: one Telegram card with the message and Sent/Skip buttons
    with time_machine.travel(datetime(2026, 12, 3, 10, 0, tzinfo=clock.TZ), tick=True):
        assert await w.jobs.owner_followup_digest() == 1
    card = [m for m in w.owner.messages if m["kind"] == "buttons"][-1]
    assert "@ktx.test" in card["text"] and seq[0]["message"] in card["text"]
    fid = seq[0]["follow_up_id"]
    await w.bot.handle_update({"callback_query": {"id": "c", "data": f"{fid}:sent",
                                                  "message": {"chat": {"id": 9229985118}, "message_id": card["id"]}}})
    assert [f for f in w.rows("Follow-ups") if f["follow_up_id"] == fid][0]["status"] == "Sent"
    # customer replies -> remaining follow-ups are cancelled
    with time_machine.travel(datetime(2026, 12, 4, 9, 0, tzinfo=clock.TZ), tick=True):
        w.claude.queue([say("ok")])
        w.composio.customer_says("instagram", "181", "actually yes, saturday?")
        await w.tick()
    statuses = {f["follow_up_id"]: f["status"] for f in w.rows("Follow-ups") if f["type"] == "quote_follow_up"}
    assert sorted(statuses.values()) == ["Cancelled", "Cancelled", "Sent"]


async def test_activity_feed_for_booking(frozen):
    w = World()
    await w.start()
    await booking_conversation(w)
    await w.dex.activity.drain()
    shorts = [r["short"] for r in w.rows("Activity")][::-1]  # oldest first
    assert shorts[:3] == ["New DM from Maria G!", "Quoted $310", "Replied to Maria G"]
    assert any(s.startswith("Booked ") and s.endswith("✅") for s in shorts)


async def test_owner_asks_open_times_without_address(frozen):
    """Atlas/owner: 'what's open Saturday?' has no customer address; slots use home base instead of crashing."""
    w = World()
    w.claude.queue([tool("find_available_slots", service="full", size_class="sedan", addons=[], condition="standard",
                         address=None, earliest_date=None, latest_date=None, time_of_day="any", max_results=3)],
                   [say("done")])
    await w.runner.run_owner_command("what's open this week for a full detail?", quiet=True)
    res = w.claude.last_tool_results()[0]
    assert not res["is_error"] and len(res["slots"]) >= 1
