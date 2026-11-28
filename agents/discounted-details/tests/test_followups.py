"""The manual follow-up workflow (docs/FOLLOWUPS.md): one daily list, grouped by platform, numbered cards,
confirm one / some / all, 3-attempt limit, conversation checks, dedupe, replies."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import time_machine

from app import clock
from tests.fakes import World, say, tool

NOW = datetime(2026, 12, 3, 10, 0, tzinfo=clock.TZ)  # Thu 10:00 AM
CHAT = {"id": 9229985118}


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=False) as t:
        yield t


class Conversations:
    """Fake 'latest messages' lookup. Unknown customers: an empty conversation."""

    def __init__(self):
        self.data: dict[tuple[str, str], list | None] = {}

    async def latest_messages(self, channel, sender_id):
        return self.data.get((channel, str(sender_id)), [])


def world():
    w = World()
    w.dex.followups.conversations = conv = Conversations()
    return w, conv


async def customer(w, **f):
    return await w.crm.create_customer({"lead_status": "Quoted", **f})


async def fu(w, cid, *, due=None, type="quote_follow_up", sender="owner", channel="instagram", msg="", **extra):
    return await w.crm.create_follow_up({"customer_id": cid, "customer_name": "", "type": type,
                                         "due_at": due or (clock.now() - timedelta(hours=1)), "sender": sender,
                                         "channel": channel, "message": msg, **extra})


def row(w, fid):
    return next(r for r in w.rows("Follow-ups") if r["follow_up_id"] == fid)


def cards(w):
    return [m for m in w.owner.messages if m["kind"] == "card"]


async def tap(w, data, mid=1):
    await w.bot.handle_update({"callback_query": {"id": "x", "data": data, "message": {"chat": CHAT, "message_id": mid}}})


# ------------------------------------------------------------------ the full sales sequence
async def test_quote_to_three_attempts_then_inactive():
    start = datetime(2026, 12, 1, 8, 0, tzinfo=clock.TZ)
    with time_machine.travel(start, tick=False) as t:
        w, _ = world()
        await w.start()
        q = dict(service="interior", size_class="sedan", addons=[], condition="standard")
        w.claude.queue([tool("quote_price", **q, address="412 Elm St, Kyle, TX 78640")],
                       [tool("send_reply", customer_id=None, text="An Interior Detail for your Camry is $165.")],
                       [say("ok")])
        w.composio.customer_says("instagram", "181", "price for interior on my camry?", name="maria.g")
        await w.tick()
        cid = (await w.crm.list_customers())[0]["customer_id"]
        await w.crm.update_customer(cid, {"instagram_username": "maria.g"})
        t.move_to(start + timedelta(hours=25))
        await w.jobs.quote_checkins()
        await w.jobs.quote_checkins()  # idempotent
        seq = [r for r in w.rows("Follow-ups") if r["category"] == "sales"]
        assert len(seq) == 1 and seq[0]["attempt"] == 1 and str(seq[0]["due_at"])[:10] == "2026-12-03"

        t.move_to(datetime(2026, 12, 3, 10, 0, tzinfo=clock.TZ))
        assert await w.dex.followups.send_report() == 1
        card = cards(w)[-1]
        assert "1. maria.g" in card["text"] and "Sales follow-up 1/3" in card["text"] and "$165" in card["text"]
        kb = card["keyboard"]
        assert kb[0][0]["copy_text"]["text"] == seq[0]["message"] and kb[0][1]["url"] == "https://ig.me/m/maria.g"
        fid = seq[0]["follow_up_id"]
        await tap(w, f"{fid}:sent")
        assert row(w, fid)["status"] == "Sent" and row(w, fid)["sent_at"] and row(w, fid)["completed_by"] == "owner"
        assert any(m["message_id"] == f"manual:{fid}" for m in w.rows("Messages"))
        nxt = [r for r in w.rows("Follow-ups") if r["status"] == "Open"]
        assert len(nxt) == 1 and nxt[0]["attempt"] == 2 and str(nxt[0]["due_at"])[:10] == "2026-12-06"
        await tap(w, f"{fid}:sent")  # repeated confirmation: no double count, no extra row
        assert "already marked sent" in w.owner.texts("edit")[-1]
        assert len([r for r in w.rows("Follow-ups") if r["status"] == "Open"]) == 1

        t.move_to(datetime(2026, 12, 6, 10, 0, tzinfo=clock.TZ))
        await w.dex.followups.send_report()
        assert "Sales follow-up 2/3" in cards(w)[-1]["text"]
        assert "1. " in await w.bot.handle_text("/done 1")
        third = [r for r in w.rows("Follow-ups") if r["status"] == "Open"][0]
        assert third["attempt"] == 3 and str(third["due_at"])[:10] == "2026-12-08"

        t.move_to(datetime(2026, 12, 8, 10, 0, tzinfo=clock.TZ))
        await w.dex.followups.send_report()
        reply = await w.bot.handle_text("done 1")
        assert "3/3" in reply and "inactive" in reply
        assert not [r for r in w.rows("Follow-ups") if r["status"] == "Open"]  # no 4th attempt
        assert (await w.crm.get_customer(cid))["lead_status"] == "Inactive"


async def test_keep_following_up_tag_is_an_authorized_exception(frozen):
    w, _ = world()
    cid = await customer(w, instagram_igsid="9", tags="keep_following_up")
    fid = await fu(w, cid, attempt=3, msg="Last check-in!")
    await w.dex.followups.send_report()
    await w.dex.followups.complete(fid)
    assert (await w.crm.get_customer(cid))["lead_status"] == "Quoted"
    assert [r for r in w.rows("Follow-ups") if r["status"] == "Open"][0]["attempt"] == 4


# ------------------------------------------------------------------ replies and conversation checks
async def test_customer_reply_cancels_sales_keeps_others_and_reactivates(frozen):
    w, _ = world()
    w.composio.customer_says("instagram", "55", "hey", name="sam", at=NOW - timedelta(days=5))
    cid = await customer(w, instagram_igsid="55", lead_status="Inactive")
    sales = await fu(w, cid, due=NOW + timedelta(days=1), msg="checking in")
    review = await fu(w, cid, type="review_request", due=NOW + timedelta(days=1), msg="review?")
    await w.start()
    w.claude.queue([say("ok")])
    w.composio.customer_says("instagram", "55", "actually can I book saturday?")
    await w.tick()
    assert row(w, sales)["status"] == "Cancelled" and "replied" in row(w, sales)["notes"].lower()
    assert row(w, review)["status"] == "Open"
    assert (await w.crm.get_customer(cid))["lead_status"] == "Quoted"


async def test_latest_conversation_is_checked_before_listing(frozen):
    w, conv = world()
    replied = await customer(w, instagram_igsid="1", instagram_username="a")
    unknown = await customer(w, messenger_psid="2", first_name="Bo")
    maybe_sent = await customer(w, instagram_igsid="3", instagram_username="c")
    f1 = await fu(w, replied, msg="Following up on your quote!")
    f2 = await fu(w, unknown, channel="messenger", msg="Hi Bo, following up!")
    f3 = await fu(w, maybe_sent, msg="Hi! Just following up on the detail quote.")
    conv.data[("instagram", "1")] = [{"at": NOW - timedelta(days=2), "from_business": True, "text": "quote", "mid": "a"},
                                     {"at": NOW - timedelta(hours=2), "from_business": False, "text": "yes pls",
                                      "mid": "b"}]
    conv.data[("messenger", "2")] = None  # can't check
    conv.data[("instagram", "3")] = [{"at": NOW - timedelta(minutes=30), "from_business": True,
                                      "text": "Hi! Just following up on the detail quote.", "mid": "m9"}]
    items = await w.dex.followups.due_items()
    ids = {i.fid: i for i in items}
    assert f1 not in ids and row(w, f1)["status"] == "Cancelled"  # they replied: no follow-up
    assert "Couldn't check the latest messages" in " ".join(ids[f2].notes)
    assert "already sent" in " ".join(ids[f3].notes) and row(w, f3)["status"] == "Open"  # never assumed


# ------------------------------------------------------------------ dedupe, filters, grouping
async def test_dedupe_filters_overdue_and_grouping(frozen):
    w, _ = world()
    ig = await customer(w, instagram_igsid="10", instagram_username="lexi")
    fb = await customer(w, messenger_psid="20", first_name="Jo")
    txt = await customer(w, phone="+15125550100", first_name="Al", lead_status="Customer")
    wa = await customer(w, phone="+15125550111", first_name="Ty")
    dnc = await customer(w, instagram_igsid="30", do_not_contact=True)
    booked = await customer(w, instagram_igsid="40", lead_status="Booked")
    a1 = await fu(w, ig, msg="Hi Lexi!", due=NOW - timedelta(days=3))
    a_dup = await fu(w, ig, sender="agent", msg="Hi Lexi (dup)")  # same customer + category, another source
    b = await fu(w, fb, channel="messenger", msg="Hi Jo!")
    appt = await fu(w, fb, type="appointment_check", channel="messenger", msg="Still good for tomorrow?")
    c = await fu(w, txt, type="review_request", channel="sms", msg="Mind leaving a review?")
    d = await fu(w, wa, channel="whatsapp", msg="Hi Ty!")
    gone1 = await fu(w, dnc, msg="x")
    gone2 = await fu(w, booked, msg="y")
    future = await fu(w, ig, type="rebook", due=NOW + timedelta(days=2), msg="z")
    items = await w.dex.followups.due_items()
    order = [(i.platform, i.fid) for i in items]
    assert order == [("instagram", a1), ("messenger", appt), ("messenger", b), ("sms", c), ("whatsapp", d)]
    assert [i.number for i in items] == [1, 2, 3, 4, 5]
    assert row(w, a_dup)["status"] == "Cancelled" and a1 in row(w, a_dup)["notes"]
    assert row(w, gone1)["status"] == "Cancelled" and row(w, gone2)["status"] == "Cancelled"
    assert row(w, future)["status"] == "Open"
    first = items[0]
    assert first.priority == "high" and "Overdue 3 days" in first.context
    assert items[1].category == "appointment" and items[1].attempt == 0  # separate from sales attempts
    assert items[3].category == "review" and items[3].attempt == 0
    assert items[4].link.startswith("https://wa.me/15125550111?text=Hi%20Ty")


async def test_auto_sendable_follow_ups_are_left_to_dex(frozen):
    w, _ = world()
    await w.start()
    w.composio.customer_says("instagram", "77", "how much?", name="kim", at=NOW - timedelta(hours=2))
    w.claude.queue([say("ok")])
    await w.tick()
    cid = (await w.crm.list_customers())[0]["customer_id"]
    await fu(w, cid, sender="agent", msg="Following up!")
    assert await w.dex.followups.due_items() == []  # window open: Dex sends it (or asks for approval)


# ------------------------------------------------------------------ report + completion
async def test_report_partial_and_bulk_completion(frozen):
    w, _ = world()
    ids = []
    for n in range(3):
        cid = await customer(w, instagram_igsid=str(100 + n), instagram_username=f"u{n}")
        ids.append(await fu(w, cid, msg=f"Hi u{n}!"))
    assert await w.dex.followups.send_report() == 3
    summary = [t for t in w.owner.texts("notify") if "to send" in t][-1]
    assert "3 to send: Instagram 3" in summary and "/done 1 3" in summary
    assert "📸 Instagram (3)" in w.owner.texts("notify")
    out = await w.bot.handle_text("/done 1 3")
    assert "1 left" in out
    assert [row(w, f)["status"] for f in ids] == ["Sent", "Open", "Sent"]
    assert row(w, ids[1])["attempt"] == 1  # untouched, not counted
    again = await w.bot.handle_text("/done 1")
    assert "Not in today's list" in again and len([r for r in w.rows("Follow-ups") if r["attempt"] == 2]) == 2
    await tap(w, "FU:all")
    assert "Mark all 1 remaining" in [m for m in w.owner.messages if m["kind"] == "buttons"][-1]["text"]
    assert row(w, ids[1])["status"] == "Open"  # nothing changes until confirmed
    await tap(w, "FU:yes")
    assert row(w, ids[1])["status"] == "Sent" and "All caught up" in w.owner.texts("notify")[-1]


async def test_skip_does_not_count_or_schedule(frozen):
    w, _ = world()
    cid = await customer(w, instagram_igsid="5")
    fid = await fu(w, cid, msg="Hi!")
    await w.dex.followups.send_report()
    assert "skipped" in await w.bot.handle_text("/skip 1")
    assert row(w, fid)["status"] == "Skipped" and not [r for r in w.rows("Follow-ups") if r["status"] == "Open"]


async def test_all_caught_up_and_report_time(frozen):
    from app.jobs.tasks import send_followup_report_if_due
    w = World(settings_overrides={"followup_report_time": "11:30"})
    assert await send_followup_report_if_due(w.dex, "🧭") == 0 and not w.owner.messages  # before 11:30
    with time_machine.travel(NOW.replace(hour=11, minute=35), tick=False):
        await send_followup_report_if_due(w.dex, "🧭")
        await send_followup_report_if_due(w.dex, "🧭")  # once a day
    assert len(w.owner.messages) == 1 and "all caught up" in w.owner.texts()[0]


async def test_morning_brief_has_no_follow_up_section(frozen):
    w, _ = world()
    cid = await customer(w, instagram_igsid="5")
    await fu(w, cid, msg="Hi!")
    text = await w.jobs.summary_text()
    assert "Send today" not in text and "Follow-ups" not in text
