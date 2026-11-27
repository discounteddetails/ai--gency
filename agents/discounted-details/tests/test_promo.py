"""Comment-to-DM promos and offer handling."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import time_machine

from app import clock
from app.jobs.comment_watcher import CommentWatcher
from app.services.promo import active_promo, amounts, keyword_hit
from tests.fakes import IG_ID, PAGE_ID, World

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=clock.TZ)
PROMO = {"promo_keyword": "FRIDAY", "promo_ends": "2026-11-30",
         "promo_dm": "Thanks for commenting! 🖤 Free pet hair removal ($25 value) with any Interior or Full Detail "
                     "booked by Monday 11/30. What car do you have?",
         "promo_details": "Free pet hair removal ($25 value) or stain treatment ($35 value) with any Interior or Full "
                          "Detail booked by 11/30."}


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=False) as t:
        yield t


def iso(dt):
    return dt.astimezone(clock.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


class Social:
    """Adds posts/comments and private replies to the fake Composio."""

    def __init__(self, w: World):
        self.w, self.ig, self.fb, self.replies = w, {}, {}, []
        self.fail_reply = False
        self._exec, self._proxy = w.composio.execute, w.composio.proxy
        w.composio.execute, w.composio.proxy = self.execute, self.proxy

    def comment(self, net, post, cid, text, from_id, name="sam", ago=timedelta(minutes=5)):
        (self.ig if net == "ig" else self.fb).setdefault(post, []).append(
            {"id": cid, "text": text, "from_id": from_id, "name": name, "at": iso(clock.now() - ago)})

    async def execute(self, slug, args):
        if slug == "INSTAGRAM_GET_IG_USER_MEDIA":
            return {"data": {"data": [{"id": p, "comments_count": len(c)} for p, c in self.ig.items()]}}
        if slug == "INSTAGRAM_GET_IG_MEDIA_COMMENTS":
            return {"data": {"data": [{"id": c["id"], "text": c["text"], "timestamp": c["at"],
                                       "from": {"id": c["from_id"], "username": c["name"]}}
                                      for c in self.ig[args["ig_media_id"]]]}}
        if slug == "FACEBOOK_GET_PAGE_POSTS":
            return {"data": [{"id": p, "comments": {"summary": {"total_count": len(c)}}} for p, c in self.fb.items()]}
        if slug == "FACEBOOK_GET_COMMENTS":
            return {"data": [{"id": c["id"], "message": c["text"], "created_time": c["at"],
                              "from": {"id": c["from_id"], "name": c["name"]}} for c in self.fb[args["object_id"]]]}
        return await self._exec(slug, args)

    async def proxy(self, toolkit, endpoint, method, body):
        if "comment_id" not in body.get("recipient", {}):
            return await self._proxy(toolkit, endpoint, method, body)
        if self.fail_reply:
            from app.services.composio_client import ComposioError
            raise ComposioError("(#10) permission")
        cid = body["recipient"]["comment_id"]
        who = next(c["from_id"] for src in (self.ig, self.fb) for cs in src.values() for c in cs if c["id"] == cid)
        self.replies.append({"toolkit": toolkit, "endpoint": endpoint, "comment_id": cid, "text": body["message"]["text"]})
        return {"recipient_id": who, "message_id": f"pr_{len(self.replies)}"}


def setup(promo=PROMO):
    w = World(settings_overrides=promo)
    return w, Social(w), CommentWatcher(w.dex, ig_user_id=IG_ID, page_id=PAGE_ID)


def test_promo_parsing():
    assert active_promo(PROMO, NOW)["keyword"] == "FRIDAY"
    assert active_promo(PROMO, NOW + timedelta(days=3, hours=8)) is not None  # Mon 11/30 11 PM: still on
    assert active_promo(PROMO, NOW + timedelta(days=4)) is None  # Tue 12/1: over
    assert active_promo({**PROMO, "promo_ends": "11/30/26"}, NOW) is not None
    assert active_promo({**PROMO, "promo_ends": "whenever"}, NOW) is None
    assert active_promo({"promo_keyword": "", "promo_details": ""}, NOW) is None
    assert amounts(active_promo(PROMO, NOW)) == {25, 35}
    assert keyword_hit("Friday!! 🙌", "FRIDAY") and keyword_hit("friday", "FRIDAY")
    assert not keyword_hit("fridays are slow", "FRIDAY") and not keyword_hit("nice work", "FRIDAY")


async def test_keyword_comments_get_one_private_dm(frozen):
    w, s, watcher = setup()
    s.comment("ig", "m1", "c1", "FRIDAY 🙌", "u1", "maria")
    s.comment("ig", "m1", "c2", "so clean!", "u2")
    s.comment("ig", "m1", "c3", "friday", "u1", "maria")  # same person again: no second DM
    s.comment("fb", "p1", "c4", "Friday please", "f9", "Jo B")
    s.comment("ig", "m1", "c5", "FRIDAY", IG_ID)  # our own account
    s.comment("ig", "m1", "c6", "FRIDAY", "u7", ago=timedelta(days=8))  # too old for a private reply
    assert await watcher.run_once() == 2
    assert [r["comment_id"] for r in s.replies] == ["c1", "c4"]
    assert s.replies[0]["endpoint"] == "/me/messages" and s.replies[1]["endpoint"] == f"/{PAGE_ID}/messages"
    assert s.replies[0]["text"] == PROMO["promo_dm"]
    assert await watcher.run_once() == 0  # nothing is ever sent twice
    # the DM is recorded as Dex's, so the poller won't think the owner took over
    assert w.db.was_sent_by_dex("pr_1")
    assert any("maria commented \"FRIDAY\"" in t for t in w.owner.texts("notify"))
    await w.dex.activity.drain()
    assert w.rows("Activity")[0]["short"] == "Sent Jo B the FRIDAY offer"


async def test_no_dms_when_promo_off_ended_or_paused(frozen):
    w, s, watcher = setup({**PROMO, "promo_keyword": ""})
    s.comment("ig", "m1", "c1", "FRIDAY", "u1")
    assert await watcher.run_once() == 0
    w, s, watcher = setup({**PROMO, "promo_ends": "2026-11-26"})
    s.comment("ig", "m1", "c1", "FRIDAY", "u1")
    assert await watcher.run_once() == 0
    w, s, watcher = setup()
    w.db.kv_set("global_pause", True)
    s.comment("ig", "m1", "c1", "FRIDAY", "u1")
    assert await watcher.run_once() == 0 and s.replies == []


async def test_dnc_customer_not_messaged_and_failures_reported(frozen):
    w, s, watcher = setup()
    cid = await w.crm.create_customer({"instagram_igsid": "u1", "first_name": "Al"})
    await w.crm.update_customer(cid, {"do_not_contact": True})
    s.comment("ig", "m1", "c1", "FRIDAY", "u1")
    assert await watcher.run_once() == 0
    s.fail_reply = True
    s.comment("ig", "m1", "c2", "FRIDAY", "u2", "bo")
    assert await watcher.run_once() == 0
    assert any("Couldn't DM bo" in t for t in w.owner.texts("notify"))


async def test_dex_sees_offer_and_promo_dm_in_context(frozen):
    w, s, watcher = setup()
    s.comment("ig", "m1", "c1", "FRIDAY", "111", "mike")
    await watcher.run_once()
    await w.start()
    w.composio.customer_says("instagram", "111", "I have a 2019 Camry, how much for interior?")
    w.claude.queue([], )
    await w.tick()
    ctx = w.claude.calls[0]["messages"][-1]["content"][0]["text"]
    assert "<current_promo>" in ctx and "Free pet hair removal" in ctx and "2026-11-30" in ctx
    assert "<promo_dm_sent>" in ctx and "What car do you have?" in ctx


async def test_offer_prices_pass_the_price_guardrail(frozen):
    from tests.fakes import tool
    w, s, watcher = setup()
    await w.start()
    w.composio.customer_says("instagram", "222", "whats the black friday deal")
    w.claude.queue([tool("send_reply", customer_id=None,
                         text="Book any Interior or Full Detail by Monday and get free pet hair removal ($25 value)!")],
                   [])
    await w.tick()
    assert "$25 value" in w.composio.sent[-1]["text"]


async def test_twenty_off_quote_and_booking(frozen):
    from tests.fakes import tool
    from tests.test_e2e import ADDR, first_slot, results
    w = World(settings_overrides={**PROMO, "promo_discount": "20",
                                  "promo_details": "$20 off any detail booked by 11/30 (appointment can be any date)"})
    await w.start()
    q = dict(service="full", size_class="large_suv", addons=["pet_hair"], condition="standard")
    w.claude.queue(
        [tool("quote_price", **q, address=ADDR)],
        [tool("find_available_slots", **q, address=ADDR, earliest_date=None, latest_date=None, time_of_day="any",
              max_results=3)],
        lambda msgs: [tool("send_reply", customer_id=None,
                           text=f"A Full Detail for your Tahoe with pet hair is normally $310, $290 with our Black "
                                f"Friday $20 off. I have {first_slot(msgs)['label']}. Does that work?")],
        [],
    )
    w.composio.customer_says("instagram", "111", "how much for a full detail on my tahoe, dog hair. 412 Elm St Kyle")
    await w.tick()
    assert "$290" in w.composio.sent[-1]["text"]
    w.claude.queue(
        lambda msgs: [tool("book_appointment", slot_id=first_slot(msgs)["slot_id"], customer_name="Mike R",
                           phone="512-555-0182", address=ADDR,
                           vehicle={"year": 2020, "make": "Chevy", "model": "Tahoe", "color": None,
                                    "size_class": "large_suv"}, service="full", addons=["pet_hair"],
                           condition="standard", notes=None)],
        lambda msgs: [tool("send_reply", customer_id=None,
                           text=f"You're all set! Total: ${results(msgs)[0]['total']}, paid after the job.")],
        [],
    )
    w.composio.customer_says("instagram", "111", "yes book it, Mike R 512-555-0182")
    await w.tick()
    job = w.rows("Jobs")[0]
    assert job["total"] == 290 and job["override_price"] == 290 and job["notes"].startswith("Promo: $20 off")
    assert "$290" in w.composio.sent[-1]["text"]


async def test_story_reply_keyword_gets_offer_dm_even_in_test_mode(frozen):
    w = World(shadow=True, settings_overrides={**PROMO, "promo_discount": "20"})
    await w.start()
    w.composio.customer_says("instagram", "333", "FRIDAY 🔥", name="kayla")
    await w.tick()
    assert w.composio.sent[-1]["text"] == PROMO["promo_dm"] and w.claude.calls == []
    assert any("Dex sent them the offer" in t for t in w.owner.texts("notify"))
    assert w.db.kv_get("promo_dm_to:instagram:333")["keyword"] == "FRIDAY"
    # their next message goes to Dex as usual (with the offer in context), and FRIDAY again doesn't resend
    w.claude.queue([])
    w.composio.customer_says("instagram", "333", "friday")
    await w.tick()
    assert len(w.composio.sent) == 1 and len(w.claude.calls) == 1
    assert "<promo_dm_sent>" in w.claude.calls[0]["messages"][-1]["content"][0]["text"]


async def test_long_message_with_keyword_goes_to_dex(frozen):
    w = World(settings_overrides=PROMO)
    await w.start()
    w.claude.queue([])
    w.composio.customer_says("instagram", "444", "hey is the friday deal good for a big truck with lots of mud?")
    await w.tick()
    assert w.composio.sent == [] and len(w.claude.calls) == 1
