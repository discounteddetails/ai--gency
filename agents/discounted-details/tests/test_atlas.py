"""Atlas: delegation, task tracking, approval-gated changes, health checks, morning brief, routing."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import time_machine

from app import clock
from app.atlas.agent import Atlas
from tests.fakes import ScriptedClaude, say, tool
from tests.test_pixel import make as make_pixel
from tests.test_pixel import settle

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=clock.TZ)
CHAT = {"id": 9229985118}


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=False) as t:
        yield t


def make(tmp_path):
    w, px, nova, pub = make_pixel(tmp_path)
    from app.agent.runner import AgentRunner
    w.dex.runner = w.runner if hasattr(w, "runner") else AgentRunner(w.dex, w.claude, "m", 10_000_000)
    brain = ScriptedClaude()
    atlas = Atlas(w.dex, brain, "m", pixel=px, nova=nova, jobs=w.jobs)
    w.bot.atlas = atlas
    return w, atlas, brain, px, nova


async def test_objective_delegates_tracks_and_reports(frozen, tmp_path):
    w, atlas, brain, px, _ = make(tmp_path)
    # Dex (owner mode) answers Atlas quietly
    w.claude.queue([tool("reply_to_owner", text="3 leads this week, 1 booked (Sat 9 AM).")], [say("done")])
    brain.queue(
        [tool("update_task", title="Cyber Monday push", agents="Dex, Pixel", status="in_progress",
              next_step="Owner picks a poster")],
        [tool("delegate_to_dex", instruction="How many leads and bookings this week?"),
         tool("start_design", request="Cyber Monday poster, $20 off any detail, book by 11/30", sizes=["story"])],
        [say("• Dex: 3 leads, 1 booked.\n• Pixel: poster options coming (D0001).\n• Task T0001 open.")],
    )
    await w.bot.handle_text("Push Cyber Monday: how are leads doing, and make a 9:16 poster")
    import asyncio
    await asyncio.gather(*getattr(px, "_bg", set()))
    reports = [t for t in w.owner.texts("notify") if t.startswith("🧭")]
    assert reports and "3 leads" in reports[-1]
    assert not any("3 leads this week" in t for t in w.owner.texts("notify") if not t.startswith("🧭"))  # Dex quiet
    tool_results = brain.calls[2]["messages"][-1]["content"]
    assert any("3 leads this week" in r["content"] for r in tool_results)
    assert px.get("D0001")["data"]["formats"] == ["story"] and px.get("D0001")["status"] == "choosing"
    task = w.rows("Tasks")[0]
    assert task["task_id"] == "T0001" and task["status"] == "in_progress"
    assert "Cyber Monday push" in await w.bot.handle_text("/tasks")
    await w.dex.activity.drain()
    assert {"Atlas", "Pixel"} <= {r["agent"] for r in w.rows("Activity")}


async def test_price_change_needs_approval_then_applies(frozen, tmp_path):
    w, atlas, brain, _, _ = make(tmp_path)
    before = [r for r in w.rows("Services") if r["service_key"] == "full" and r["size_class"] == "sedan"][0]["price"]
    brain.queue(
        [tool("propose_change", summary="Full Detail sedan to $235", reason="Demand is high",
              changes=[{"tab": "Services", "key": "full", "size_class": "sedan", "field": "price", "value": 235}])],
        [say("Sent you A0001 to approve the new price.")],
    )
    await w.bot.handle_text("raise the full detail sedan price by 10")
    card = [m for m in w.owner.messages if m["kind"] == "buttons"][-1]
    assert "BUSINESS CHANGE" in card["text"] and "full sedan price → 235" in card["text"]
    after_proposal = [r for r in w.rows("Services") if r["service_key"] == "full" and r["size_class"] == "sedan"][0]
    assert after_proposal["price"] == before  # nothing changed yet
    await w.bot.handle_update({"callback_query": {"id": "1", "data": "A0001:approve",
                                                  "message": {"chat": CHAT, "message_id": 9}}})
    row = [r for r in w.rows("Services") if r["service_key"] == "full" and r["size_class"] == "sedan"][0]
    assert row["price"] == 235
    assert (await w.crm.get_services())  # cache refreshed without error


async def test_promo_change_declined_changes_nothing(frozen, tmp_path):
    w, atlas, brain, _, _ = make(tmp_path)
    tid = (await atlas.update_task(title="Extend offer"))["task_id"]
    r = await atlas.propose_change("Extend Black Friday to 12/3", "Slow week", [
        {"tab": "Settings", "key": "promo_ends", "value": "2026-12-03"}], tid)
    await w.dex.decide(r["approval_id"], "decline")
    settings = await w.crm.get_settings()
    assert settings.get("promo_ends", "") != "2026-12-03"
    assert w.rows("Tasks")[0]["status"] == "blocked"
    bad = await atlas.propose_change("x", "y", [{"tab": "Customers", "key": "C0001", "value": "x"}])
    assert "error" in bad


async def test_checks_find_operational_problems(frozen, tmp_path):
    w, atlas, _, px, nova = make(tmp_path)
    for k, v in {"promo_keyword": "FRIDAY", "promo_ends": "2026-11-20", "promo_discount": "20"}.items():
        w.store.append("Settings", {"key": k, "value": v})
    w.store.append("Services", dict(w.rows("Services")[0]))
    w.crm.invalidate_cache()
    nova._save("N0001", "post", "pending", {"media": [], "created_at": (NOW - timedelta(days=2)).isoformat()})
    nova._save("N0002", "post", "failed", {"media": [], "errors": ["IG feed: denied"],
                                            "created_at": NOW.isoformat()})
    px._save("D0009", "designing", {"created_at": (NOW - timedelta(hours=1)).isoformat()})
    issues = "\n".join(await atlas.checks())
    assert "offer ended" in issues and "duplicate row" in issues
    assert "N0001 has been waiting" in issues and "N0002 failed" in issues
    assert "D0009 got stuck" in issues and px.get("D0009")["status"] == "failed"


async def test_morning_brief_replaces_daily_summary(frozen, tmp_path):
    w, atlas, brain, _, _ = make(tmp_path)
    text = await atlas.daily_brief()
    assert "Pending approvals" in text and "Nova:" in text
    from app.jobs.scheduler import build_scheduler
    sched = build_scheduler(w.jobs, w.poller, 60, 900, atlas=atlas)
    assert sched.get_job("daily_summary").func.__name__ == "daily_brief"
    await w.bot.handle_text("/brief")
    assert w.owner.texts("notify")[-1].count("Pending approvals") == 1


async def test_names_still_go_straight_to_agents(frozen, tmp_path):
    w, atlas, brain, px, _ = make(tmp_path)
    from tests.test_pixel import RouterClient, with_router
    with_router(w, px, None, RouterClient({"agent": "pixel", "action": "new_design", "text": "gift card"}))
    await w.bot.handle_text("Pixel, gift card")
    await settle(px)
    assert brain.calls == [] and px.designer.calls[-1]["request"] == "gift card"
    w.claude.queue([])
    with_router(w, px, None, RouterClient({"agent": "dex", "action": "owner_command", "text": "who's booked"}))
    await w.bot.handle_text("Dex, who's booked tomorrow")
    assert brain.calls == [] and len(w.claude.calls) == 1


async def test_atlas_gets_team_status_for_design_feedback(frozen, tmp_path):
    w, atlas, brain, px, nova = make(tmp_path)
    from app.router import Router
    w.bot.router = Router(None, "m", pixel=px, nova=nova, dex=w.dex)
    await px.request("youtube thumbnail")
    await settle(px)
    brain.queue([tool("change_design", job_id="D0001", feedback="make it 9:16 for TikTok")], [say("Updating it.")])
    await w.bot.handle_text("make it 9:16 for TikTok")
    first = brain.calls[0]["messages"][-1]["content"][0]["text"]
    assert "pixel_current_design" in first and "D0001" in first
    assert px.designer.calls[-1]["formats"] == ["story"]


def test_atlas_prompt_has_communication_rules():
    from app.atlas.agent import system_prompt
    p = system_prompt()
    for must in ("📌 Summary", "✅ Completed", "⚠️ Needs attention", "➡️ Next steps", "1–3 sentences",
                 "Never call something done without verifying it", "plain text"):
        assert must in p
    assert "## Approval rules (hard)" in p and "delegate_to_dex" in p  # responsibilities unchanged
