"""Atlas: operations manager (docs/ATLAS_SPEC.md). Orchestrator-worker: Atlas plans and its tools are the other
agents (Dex's owner mode, Pixel's design jobs, Nova's content). It adds task tracking, approval-gated business
changes, a daily brief with health checks, and weekly improvement ideas."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import anthropic

from app import clock
from app.agent.runner import _block_to_param
from app.core import Dex
from app.services.promo import active_promo

log = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parents[2] / "docs" / "ATLAS_INSTRUCTIONS.md"
MAX_STEPS = 12
HISTORY_TURNS = 20


def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").split("\n---\n", 1)[-1].strip()


def _t(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}


S = {"type": "string"}
TOOLS = [
    _t("delegate_to_dex", "Give Dex (customers, DMs, quotes, bookings, CRM, follow-ups, customer promos) an "
       "instruction. Returns Dex's report. Dex keeps its own approval rules.", {"instruction": S}, ["instruction"]),
    _t("start_design", "Ask Pixel for a design. She sends the owner 2 options in Telegram (about a minute). "
       "sizes: any of feed (4:5 post), story (9:16 Story/Reel/TikTok), square, youtube (16:9). Leave empty to let "
       "Pixel choose.", {"request": S, "sizes": {"type": "array", "items": {"type": "string",
                                                                            "enum": ["feed", "story", "square",
                                                                                     "youtube"]}},
                         "use_recent_photos": {"type": "boolean"}}, ["request"]),
    _t("change_design", "Send the owner's change to one of Pixel's design jobs (D####).",
       {"job_id": S, "feedback": S}, ["job_id", "feedback"]),
    _t("pixel_style_rule", "Save a lasting style preference for Pixel.", {"rule": S}, ["rule"]),
    _t("nova_status", "Nova's recent and upcoming content (pending approvals, scheduled, published, failed).", {}, []),
    _t("nova_offer_story", "Have Nova make a Story image of the running offer and send it for approval.", {}, []),
    _t("business_snapshot", "Read live business data from the Sheet (the single source of truth).",
       {"section": {"type": "string", "enum": ["overview", "services", "settings", "promo", "jobs", "leads",
                                               "approvals", "follow_ups", "content", "tasks", "activity",
                                               "spending"]}},
       ["section"]),
    _t("update_task", "Create (no task_id) or update a tracked task in the Sheet's Tasks tab.",
       {"task_id": S, "title": S, "agents": S, "status": {"type": "string",
                                                         "enum": ["open", "in_progress", "waiting_on_owner",
                                                                  "blocked", "done"]},
        "next_step": S, "due": S, "note": S}, []),
    _t("propose_change", "Ask the owner to approve a change to prices, services, promos/discounts or settings. "
       "Nothing changes until they tap Approve. changes: [{tab: Settings|Services, key: setting key or "
       "service_key, size_class: (Services only, optional), field: (Services: price|duration_min|active|"
       "includes|name), value}].",
       {"summary": S, "reason": S, "changes": {"type": "array", "items": {"type": "object", "properties": {
           "tab": {"type": "string", "enum": ["Settings", "Services"]}, "key": S, "size_class": S, "field": S,
           "value": {"type": ["string", "number", "boolean"]}}, "required": ["tab", "key", "value"]}},
        "task_id": S}, ["summary", "reason", "changes"]),
]


class Atlas:
    def __init__(self, dex: Dex, client, model: str, *, pixel=None, nova=None, jobs=None):
        self.dex, self.client, self.model, self.pixel, self.nova, self.jobs = dex, client, model, pixel, nova, jobs
        dex.on_business_change = self._on_business_change

    # ------------------------------------------------------------------ conversation
    async def handle(self, text: str, context: dict[str, Any] | None = None) -> str:
        """The owner's message -> plan, delegate, check, report. Returns the report (also sent to Telegram)."""
        key = "atlas"
        runner = self.dex.runner
        if runner and not runner._budget_ok():
            await self.dex.owner.notify("⛔ Daily AI budget reached, so Atlas is paused until tomorrow. Dex, Nova and "
                                        "Pixel buttons still work.", urgent=True)
            return ""
        messages = self.dex.db.load_history(key, HISTORY_TURNS)
        turn = uuid.uuid4().hex[:12]
        ctx = f"<team_status>{json.dumps(context, default=str)}</team_status>\n" if context else ""
        user = {"role": "user", "content": [{"type": "text", "text": f"<now>{clock.label(clock.now())}</now>\n{ctx}"
                                                                     f"<owner_message>{text}</owner_message>"}]}
        messages.append(user)
        self.dex.db.append_history(key, turn, "user", user["content"])
        self.dex.activity.log("Atlas", f"New objective from you: {text[:150]}", "On it 🧭")
        report = ""
        for _ in range(MAX_STEPS):
            try:
                resp = await self.client.beta.messages.create(
                    model=self.model, max_tokens=8000,
                    system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS, messages=messages,
                    thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01", "thinking-binding-controls-2026-08-01"],
                    fallbacks="default")
            except anthropic.APIError as e:
                log.error("Atlas request failed: %s", e)
                from app.services.ai_errors import alert_credits, is_credit_error
                if is_credit_error(e):
                    await alert_credits(self.dex)
                    return ""
                report = "⚠️ Atlas hit an error reaching Claude. Try again in a minute, or message Dex/Nova/Pixel directly."
                break
            if runner:
                runner._track(getattr(resp, "usage", None))
            content = [_block_to_param(b) for b in resp.content]
            messages.append({"role": "assistant", "content": content})
            self.dex.db.append_history(key, turn, "assistant", content)
            uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                report = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
                break
            results = []
            for u in uses:
                try:
                    out = await self._tool(u.name, dict(u.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out, default=str)})
                except Exception as e:
                    log.exception("Atlas tool %s failed", u.name)
                    results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                    "content": json.dumps({"error": f"{type(e).__name__}: {e}"})})
            tool_msg = {"role": "user", "content": results}
            messages.append(tool_msg)
            self.dex.db.append_history(key, turn, "user", results)
        else:
            report = "I ran out of steps on that one. Here's where it stands: check /tasks, and tell me what to finish first."
        if report:
            await self.dex.owner.notify(f"🧭 {report}", urgent=True)
            self.dex.activity.log("Atlas", f"Reported back: {report[:200]}", "Reported to you ✅")
        return report

    # ------------------------------------------------------------------ tools
    async def _tool(self, name: str, a: dict[str, Any]) -> Any:
        dex = self.dex
        if name == "delegate_to_dex":
            if not dex.runner:
                return {"error": "Dex is offline"}
            dex.activity.log("Atlas", f"Asked Dex: {a['instruction'][:150]}", "Delegated to Dex")
            replies = await dex.runner.run_owner_command(a["instruction"], quiet=True)
            return {"dex_report": replies or ["(Dex finished without a written report)"]}
        if name == "start_design":
            if not self.pixel:
                return {"error": "Pixel is offline"}
            dex.activity.log("Atlas", f"Asked Pixel for: {a['request'][:150]}", "Delegated to Pixel")
            jid = await self.pixel.start(a["request"], a.get("sizes") or None, bool(a.get("use_recent_photos")))
            return {"job_id": jid, "status": "Pixel is designing; the owner gets 2 options in Telegram in ~1 min"}
        if name == "change_design":
            r = await self.pixel.revise(a["job_id"], a["feedback"])
            return {"ok": not r, "note": r or "Pixel is updating the design"}
        if name == "pixel_style_rule":
            return {"ok": True, "note": self.pixel.remember_rule(a["rule"])}
        if name == "nova_status":
            if not self.nova:
                return {"error": "Nova is offline"}
            cutoff = clock.now() - timedelta(days=7)
            items = [c for c in self.nova.list() if clock.parse_iso(c["data"].get("created_at") or c["created_at"])
                     >= cutoff]
            return [{"id": c["content_id"], "kind": c["kind"], "status": c["status"],
                     "scheduled_for": c["data"].get("scheduled_for", ""), "caption": c["data"].get("caption", "")[:160],
                     "where": c["data"].get("platforms") or c["data"].get("targets"),
                     "errors": c["data"].get("errors", [])} for c in items[-15:]]
        if name == "nova_offer_story":
            r = await self.nova.offer_story()
            return {"ok": not r, "note": r or "Story image sent to the owner for approval"}
        if name == "business_snapshot":
            return await self.snapshot(a["section"])
        if name == "update_task":
            return await self.update_task(**{k: a.get(k) for k in ("task_id", "title", "agents", "status",
                                                                     "next_step", "due", "note")})
        if name == "propose_change":
            return await self.propose_change(a["summary"], a["reason"], a["changes"], a.get("task_id"))
        return {"error": f"unknown tool {name}"}

    async def snapshot(self, section: str) -> Any:
        crm, now = self.dex.crm, clock.now()
        if section == "services":
            return [{k: r.get(k) for k in ("service_key", "name", "size_class", "price", "duration_min", "active")}
                    for r in await crm._read("Services")]
        if section in ("settings", "promo"):
            st = await crm.get_settings()
            if section == "promo":
                return {"active": active_promo(st), **{k: v for k, v in st.items() if k.startswith("promo_")}}
            return st
        if section == "jobs":
            rows = []
            for j in await crm.list_jobs():
                try:
                    start = clock.parse_iso(str(j.get("start")))
                except (ValueError, TypeError):
                    continue
                if now - timedelta(days=14) <= start <= now + timedelta(days=21):
                    rows.append({k: j.get(k) for k in ("job_id", "service", "start", "total", "status", "notes")})
            return rows
        if section == "leads":
            out = []
            for c in await crm.list_customers():
                out.append({"id": c.get("customer_id"), "name": self.dex.display_name(c),
                            "status": c.get("lead_status"), "first_contact": c.get("first_contact_at"),
                            "last_inbound": c.get("last_inbound_at")})
            return out[-40:]
        if section == "approvals":
            return [{"id": a["approval_id"], "type": a["type"], "summary": a["payload"].get("summary", "")}
                    for a in self.dex.db.list_approvals("pending")]
        if section == "follow_ups":
            return [{k: f.get(k) for k in ("follow_up_id", "customer_name", "type", "due_at", "sender", "status")}
                    for f in await crm.list_follow_ups(status="Open")][:40]
        if section == "content":
            return await self._tool("nova_status", {})
        if section == "tasks":
            return [r for r in await crm._read("Tasks") if r.get("status") != "done"]
        if section == "spending":
            ledger = getattr(self.dex, "ledger", None)
            if not ledger:
                return {"error": "spending tracker is off"}
            from app.services.spend import spend_text
            return await spend_text(ledger, await crm.get_settings())
        if section == "activity":
            return (await crm._read("Activity"))[:30]
        # overview
        return {"now": clock.label(now), "promo": await self.snapshot("promo"),
                "jobs": await self.snapshot("jobs"), "approvals": await self.snapshot("approvals"),
                "open_tasks": await self.snapshot("tasks"), "issues": await self.checks()}

    # ------------------------------------------------------------------ tasks
    async def update_task(self, task_id=None, title=None, agents=None, status=None, next_step=None, due=None,
                          note=None) -> dict[str, Any]:
        crm = self.dex.crm
        fields = {k: v for k, v in {"title": title, "agents": agents, "status": status, "next_step": next_step,
                                    "due": due}.items() if v}
        fields["updated_at"] = clock.now()
        if task_id:
            if note:
                rows = [r for r in await crm._read("Tasks") if r.get("task_id") == task_id]
                old = str(rows[0].get("notes", "")) if rows else ""
                fields["notes"] = (old + f"\n{clock.now():%m/%d %H:%M} {note}").strip()[-1500:]
            ok = await crm._update("Tasks", task_id, fields)
            if status == "done":
                self.dex.activity.log("Atlas", f"Closed task {task_id}: {title or ''}".strip(), "Task done ✅")
            return {"task_id": task_id, "updated": ok}
        tid = await crm._create("Tasks", {**fields, "status": status or "open", "created_at": clock.now(),
                                          "notes": f"{clock.now():%m/%d %H:%M} {note}" if note else ""})
        self.dex.activity.log("Atlas", f"Opened task {tid}: {title}", f"New task {tid}")
        return {"task_id": tid, "created": True}

    async def tasks_text(self) -> str:
        rows = [r for r in await self.dex.crm._read("Tasks") if r.get("status") != "done"]
        if not rows:
            return "🧭 No open tasks."
        icon = {"open": "⬜", "in_progress": "🔄", "waiting_on_owner": "🙋", "blocked": "⛔"}
        return "🧭 Open tasks:\n" + "\n".join(
            f"{icon.get(r.get('status'), '•')} {r['task_id']} {r.get('title', '')} ({r.get('agents', '')})"
            + (f"\n   next: {r['next_step']}" if r.get("next_step") else "") for r in rows)

    # ------------------------------------------------------------------ approvals
    async def propose_change(self, summary: str, reason: str, changes: list[dict[str, Any]],
                             task_id: str | None = None) -> dict[str, Any]:
        clean = []
        for ch in changes:
            if ch.get("tab") not in ("Settings", "Services") or not ch.get("key"):
                return {"error": f"bad change {ch}: only Settings or Services rows"}
            clean.append({k: ch.get(k) for k in ("tab", "key", "size_class", "field", "value") if ch.get(k) is not None})
        lines = [f"• {c['tab']}: {c['key']}{' ' + c['size_class'] if c.get('size_class') else ''}"
                 f"{' ' + c['field'] if c.get('field') else ''} → {c['value']}" for c in clean]
        aid = await self.dex.create_approval("business_change", None, summary=f"Atlas: {summary}",
                                             customer_request=f"Why: {reason}\n" + "\n".join(lines),
                                             payload={"changes": clean, "task_id": task_id, "reason": reason})
        if task_id:
            await self.update_task(task_id, status="waiting_on_owner", next_step=f"Owner to approve {aid}")
        self.dex.activity.log("Atlas", f"Asked you to approve {aid}: {summary}", "Needs your OK 🙋")
        return {"approval_id": aid, "status": "waiting for the owner"}

    async def _on_business_change(self, aid: str, p: dict[str, Any], approved: bool) -> None:
        self.dex.activity.log("Atlas", f"{aid} {'approved and applied' if approved else 'declined'}",
                              "Change applied ✅" if approved else "Change declined")
        if p.get("task_id"):
            await self.update_task(p["task_id"], status="in_progress" if approved else "blocked",
                                   note=f"{aid} {'approved' if approved else 'declined'}")

    # ------------------------------------------------------------------ operations
    async def checks(self) -> list[str]:
        """Cheap rule checks (no AI): problems an operations manager should catch."""
        issues: list[str] = []
        now, crm = clock.now(), self.dex.crm
        st = await crm.get_settings()
        if (st.get("promo_keyword") or st.get("promo_discount")) and not active_promo(st):
            issues.append(f"The offer ended ({st.get('promo_ends') or 'no end date'}), but promo settings are still "
                          "filled in. Clear them or set a new offer.")
        services = await crm._read("Services")
        seen: set[tuple] = set()
        for r in services:
            k = (r.get("service_key"), r.get("size_class"))
            if k in seen:
                issues.append(f"Services tab has a duplicate row for {k[0]} / {k[1]}.")
            seen.add(k)
            if str(r.get("active", True)).upper() not in ("FALSE", "0") and not str(r.get("price", "")).strip():
                issues.append(f"Services tab: {k[0]} / {k[1]} has no price.")
        stale = [a for a in self.dex.db.list_approvals("pending")
                 if (c := a.get("created_at")) and now - clock.parse_iso(str(c)) > timedelta(hours=12)]
        if stale:
            issues.append(f"{len(stale)} approval(s) waiting over 12h: " + ", ".join(a["approval_id"] for a in stale[:5]))
        if self.nova:
            for c in self.nova.list():
                created = clock.parse_iso(c["data"].get("created_at") or c["created_at"])
                if c["status"] == "failed" and now - created < timedelta(days=2):
                    issues.append(f"Nova: {c['content_id']} failed to post ({'; '.join(c['data'].get('errors', []))[:120]}).")
                if c["status"] == "pending" and now - created > timedelta(hours=24):
                    issues.append(f"Nova: {c['content_id']} has been waiting for your approval over a day.")
        if self.pixel:
            rows = self.dex.db._q("SELECT job_id FROM pixel_jobs WHERE status='designing'")
            for r in rows:
                job = self.pixel.get(r["job_id"])
                if job and now - clock.parse_iso(job["data"]["created_at"]) > timedelta(minutes=20):
                    self.pixel._save(job["job_id"], "failed", job["data"])
                    issues.append(f"Pixel: {job['job_id']} got stuck and was stopped. Ask again if you still need it.")
        for t in await crm._read("Tasks"):
            if t.get("status") in ("done", "") or not t.get("updated_at"):
                continue
            try:
                if now - clock.parse_iso(str(t["updated_at"])) > timedelta(days=3):
                    issues.append(f"Task {t['task_id']} ({t.get('title', '')}) hasn't moved in 3+ days.")
            except (ValueError, TypeError):
                pass
        used = int(self.dex.db.kv_get(f"tokens_{now:%Y%m%d}", 0) or 0)
        if used > 3_000_000:
            issues.append(f"AI usage is high today ({used:,} weighted tokens).")
        return issues

    async def daily_brief(self) -> str:
        """6:30 AM: Dex's summary + Nova/Pixel/tasks + health checks; Mondays add improvement ideas."""
        parts = [await self.jobs.summary_text()] if self.jobs else []
        if self.nova:
            today = clock.now().date()
            sched = [c for c in self.nova.list(status="scheduled") if c["data"].get("scheduled_for")
                     and clock.parse_iso(c["data"]["scheduled_for"]).date() == today]
            pend = self.nova.list(status="pending")
            parts.append(f"📸 Nova: {len(sched)} post(s) going out today, {len(pend)} waiting for your OK")
        open_tasks = [r for r in await self.dex.crm._read("Tasks") if r.get("status") not in ("done", "")]
        if open_tasks:
            parts.append("🧭 Open tasks: " + ", ".join(f"{r['task_id']} {r.get('title', '')}" for r in open_tasks[:6]))
        ledger = getattr(self.dex, "ledger", None)
        if ledger:
            y = clock.now().date() - timedelta(days=1)
            m0 = clock.now().date().replace(day=1)
            yd, mo = sum(ledger.by_agent(y, y).values()), sum(ledger.by_agent(m0, clock.now().date()).values())
            parts.append(f"💰 AI spend: ${yd:.2f} yesterday · ${mo:.2f} this month (/spend for details)")
        issues = await self.checks()
        if issues:
            parts.append("⚠️ Needs attention:\n" + "\n".join(f"• {i}" for i in issues[:8]))
        if clock.now().weekday() == 0:
            ideas = await self.weekly_ideas()
            if ideas:
                parts.append("💡 This week:\n" + ideas)
        text = "\n\n".join(parts)
        await self.dex.owner.notify(text, urgent=True)
        self.dex.activity.log("Atlas", "Sent your morning brief" + (f" ({len(issues)} issue(s))" if issues else ""),
                              "Morning brief sent ☀️")
        return text

    async def weekly_ideas(self) -> str:
        try:
            data = {"jobs": await self.snapshot("jobs"), "leads": await self.snapshot("leads"),
                    "content": await self._tool("nova_status", {}), "promo": await self.snapshot("promo"),
                    "services": await self.snapshot("services")}
            resp = await self.client.messages.create(
                model=self.model, max_tokens=1500, output_config={"effort": "medium"},
                messages=[{"role": "user", "content": system_prompt() + "\n\nLast weeks' data:\n"
                           + json.dumps(data, default=str)[:30000]
                           + "\n\nGive the owner the 2 most valuable concrete actions for this week, one line "
                             "each, with the expected effect. Anything involving prices, discounts or spending "
                             "must say it needs their approval."}])
            return "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        except Exception as e:
            log.warning("weekly ideas failed: %s", e)
            return ""
