"""Scheduled jobs (PRD §5.5). Follow-up messages use fixed templates (AGENT_INSTRUCTIONS §8), which keeps them cheap
and predictable. Anything Dex can't send by DM goes to the owner's follow-up list."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app import clock
from app.core import Dex
from app.services.channels import dm_window_state
from app.services.composio_client import ComposioError
from app.webhooks.telegram import jobs_text

log = logging.getLogger(__name__)
SERVICE_LABEL = {"exterior": "Exterior Detail", "interior": "Interior Detail", "full": "Full Detail"}
CHANNEL_NAMES = {"instagram": "Instagram", "messenger": "Messenger"}
QUOTE_SEQUENCE_DAYS = (2, 5, 7)


def quote_followup_text(dex: Dex, customer: dict[str, Any], step: int) -> str:
    q = dex.db.kv_get(f"last_quote:{customer['customer_id']}", {}) or {}
    first = str(customer.get("first_name") or "").strip()
    hi = f"Hi {first}!" if first else "Hi!"
    service = q.get("service_name") or "detail"
    car = q.get("vehicle") or "your car"
    car_ref = f"your {car}" if q.get("vehicle") else car
    price = f" (${q['total']})" if q.get("total") else ""
    if step <= 2:
        return f"{hi} Just following up on the {service} quote for {car_ref}{price}. I have a few openings this week if you'd like to grab one."
    if step <= 5:
        return (f"Hey{' ' + first if first else ''}, wanted to check back in about getting {car_ref} detailed. "
                "Happy to work around your schedule. Want me to send over a few times?")
    return (f"{hi} Last check-in from me. If you'd still like the {service} for {car_ref}, just reply here and I'll "
            "get you scheduled. No worries if now isn't the right time!")


def _dt(v: Any) -> datetime | None:
    try:
        return clock.parse_iso(str(v)) if v not in (None, "") else None
    except ValueError:
        return None


async def _vehicle(dex: Dex, job: dict[str, Any] | None) -> str:
    if not job:
        return "car"
    for v in await dex.crm.list_vehicles(job["customer_id"]):
        if v["vehicle_id"] == job.get("vehicle_id"):
            return " ".join(str(x) for x in (v.get("make"), v.get("model")) if x) or "car"
    return "car"


async def render(dex: Dex, fu_type: str, customer: dict[str, Any], job: dict[str, Any] | None) -> str:
    settings = await dex.crm.get_settings()
    first = str(customer.get("first_name") or "").strip()
    hi = f"Hi {first}!" if first else "Hi!"
    service = SERVICE_LABEL.get(str((job or {}).get("service")), "detail")
    vehicle = await _vehicle(dex, job)
    start = _dt((job or {}).get("start"))
    if fu_type == "reminder" and start:
        return (f"{hi} Just a reminder that Discounted Details is scheduled for your {service} tomorrow, "
                f"{start.strftime('%a, %b')} {start.day}, at {start.strftime('%-I:%M %p')} at {job.get('address')}. "
                "If anything changes, just message us here. See you then!")
    if fu_type == "review_request":
        url = str(settings.get("facebook_review_url") or "")
        link = (" If you have a minute, a quick review on our Facebook page would mean a lot to a small local "
                f"business: {url}") if url else ""
        return f"Hi {first or 'there'}, thanks again for choosing Discounted Details! I hope you're loving how your " \
               f"{vehicle} turned out.{link}"
    if fu_type == "rebook":
        return (f"{hi} It's been about 3 months since your {vehicle}'s {service}. Want to get it back on the "
                "schedule? I have openings next week.")
    if fu_type == "quote_follow_up":
        return (f"Hi {first or 'there'}, just checking in on the quote for your detail. I still have a few openings "
                "this week if you'd like to grab one!")
    return ""


class Jobs:
    def __init__(self, dex: Dex, healthcheck_url: str | None = None):
        self.dex = dex
        self.healthcheck_url = healthcheck_url

    # ------------------------------------------------------------ follow-ups
    async def send_due_agent_followups(self) -> int:
        dex, now, n = self.dex, clock.now(), 0
        for fu in await dex.crm.list_follow_ups(status="Open", sender="agent"):
            due = _dt(fu.get("due_at"))
            if not due or due > now:
                continue
            customer = await dex.crm.get_customer(fu["customer_id"])
            if not customer or dex.crm.is_dnc(customer):
                await dex.crm.update_follow_up(fu["follow_up_id"], {"status": "Cancelled"})
                continue
            job = await dex.crm.get_job(fu["job_id"]) if fu.get("job_id") else None
            if job and job.get("status") in ("Cancelled", "No-show"):
                await dex.crm.update_follow_up(fu["follow_up_id"], {"status": "Cancelled"})
                continue
            text = fu.get("message") or await render(dex, fu["type"], customer, job)
            thread = dex.thread_for(customer)
            window, _ = dm_window_state(thread.get("last_inbound_at") if thread else None, now)
            sent = False
            if thread and window == "open" and not dex.is_paused(thread) and not dex.paused and not dex.shadow:
                try:
                    await dex._dm_send(customer, thread["channel"], thread["sender_id"], text)
                    sent = True
                except ComposioError as e:
                    log.info("follow-up DM failed, handing to owner: %s", e)
            if sent:
                await dex.crm.update_follow_up(fu["follow_up_id"], {"status": "Sent", "sent_at": now, "message": text})
            else:
                await dex.crm.update_follow_up(fu["follow_up_id"], {"sender": "owner", "message": text})
            n += 1
        return n

    async def quote_checkins(self) -> None:
        dex, now = self.dex, clock.now()
        for t in dex.db.list_threads():
            if not t.get("customer_id") or not t.get("last_quote_at"):
                continue
            c = await dex.crm.get_customer(t["customer_id"])
            if not c or dex.crm.is_dnc(c) or c.get("lead_status") != "Quoted":
                continue
            age = now - t["last_inbound_at"] if t.get("last_inbound_at") else timedelta(0)
            if not t["quote_checkin_sent"] and timedelta(hours=18) <= age <= timedelta(hours=22) \
                    and not dex.is_paused(t) and not dex.paused and not dex.shadow:
                try:
                    await dex._dm_send(c, t["channel"], t["sender_id"], await render(dex, "quote_follow_up", c, None))
                except ComposioError:
                    pass
                dex.db.upsert_thread(t["thread_id"], quote_checkin_sent=True)
            # Once they've gone quiet for 24h after a quote, queue the day 2/5/7 follow-ups (once per quiet spell).
            if t.get("last_inbound_at") and age >= timedelta(hours=24) and \
                    dex.db.kv_get(f"quote_seq:{t['thread_id']}") != t["last_inbound_at"].isoformat():
                await self._create_quote_sequence(c, t)
            if age >= timedelta(days=14):
                await dex.crm.update_customer(c["customer_id"], {"lead_status": "Lost"})

    async def _create_quote_sequence(self, c: dict[str, Any], t: dict[str, Any]) -> None:
        """Owner-sent follow-ups on days 2, 5 and 7 after a quote (Dex drafts them; the owner sends from the app,
        because Meta doesn't allow automated follow-ups outside the 24-hour window)."""
        dex = self.dex
        quiet_since = t["last_inbound_at"]  # days count from their last message
        for step in QUOTE_SEQUENCE_DAYS:
            due = (quiet_since + timedelta(days=step)).replace(hour=10, minute=0, second=0, microsecond=0)
            await dex.crm.create_follow_up({
                "customer_id": c["customer_id"], "customer_name": dex.display_name(c), "type": "quote_follow_up",
                "due_at": due, "sender": "owner", "channel": t["channel"], "created_by": "scheduler",
                "message": quote_followup_text(dex, c, step), "notes": f"Quote follow-up, day {step}"})
        dex.db.kv_set(f"quote_seq:{t['thread_id']}", quiet_since.isoformat())

    async def owner_followup_digest(self) -> int:
        """10 AM: post every owner follow-up due today to Telegram, one message each, ready to copy and send."""
        dex, now, n = self.dex, clock.now(), 0
        due = [f for f in await dex.crm.list_follow_ups(status="Open", sender="owner")
               if (d := _dt(f.get("due_at"))) and d <= now.replace(hour=23, minute=59)]
        if not due:
            return 0
        await dex.owner.notify(f"📋 {len(due)} message{'s' if len(due) != 1 else ''} to send today. Copy each one into "
                               "the customer's chat, then tap ✅ Sent.", urgent=True)
        for f in due:
            c = await dex.crm.get_customer(f["customer_id"]) or {}
            where = (f"Instagram @{c['instagram_username']}" if f.get("channel") == "instagram" and c.get("instagram_username")
                     else f"{CHANNEL_NAMES.get(f.get('channel', ''), 'DM')}: {f.get('customer_name') or dex.display_name(c)}")
            label = (f.get("notes") or f.get("type", "")).replace("_", " ")
            await dex.owner.post_buttons(f"{f['follow_up_id']} · {label}\nSend to {where}:\n\n{f.get('message', '')}",
                                         [[("✅ Sent", f"{f['follow_up_id']}:sent"), ("⏭ Skip", f"{f['follow_up_id']}:skip")]])
            n += 1
        return n

    # ------------------------------------------------------------ jobs lifecycle
    async def auto_complete_jobs(self) -> list[str]:
        dex, now, done = self.dex, clock.now(), []
        for j in await dex.crm.list_jobs():
            end = _dt(j.get("end"))
            if j.get("status") == "Booked" and end and end < now:
                await dex.crm.update_job(j["job_id"], {"status": "Completed", "completed_by": "auto"})
                done.append(j["job_id"])
        await self._refresh_customer_stats()
        return done

    async def _refresh_customer_stats(self) -> None:
        dex = self.dex
        jobs = [j for j in await dex.crm.list_jobs() if j.get("status") == "Completed"]
        by_c: dict[str, list[dict[str, Any]]] = {}
        for j in jobs:
            by_c.setdefault(j["customer_id"], []).append(j)
        for cid, js in by_c.items():
            c = await dex.crm.get_customer(cid)
            if not c:
                continue
            last = max(str(j.get("start", ""))[:10] for j in js)
            total = sum(int(float(j.get("total") or 0)) for j in js)
            fields = {"total_jobs": len(js), "lifetime_value": total, "last_job_date": last}
            if c.get("lead_status") not in ("DNC",):
                fields["lead_status"] = "Customer"
            if any(str(c.get(k)) != str(v) for k, v in fields.items()):
                await dex.crm.update_customer(cid, fields)

    async def post_job_followups(self) -> int:
        dex, now, n = self.dex, clock.now(), 0
        done: list[str] = dex.db.kv_get("post_job_done", [])
        settings = await dex.crm.get_settings()
        for j in await dex.crm.list_jobs():
            if j.get("status") != "Completed" or j["job_id"] in done:
                continue
            c = await dex.crm.get_customer(j["customer_id"])
            done.append(j["job_id"])
            if not c or dex.crm.is_dnc(c):
                continue
            base = {"customer_id": c["customer_id"], "customer_name": dex.display_name(c), "job_id": j["job_id"],
                    "sender": "agent", "created_by": "scheduler",
                    "channel": (dex.thread_for(c) or {}).get("channel", "")}
            recent_review = any(f.get("type") == "review_request" and (d := _dt(f.get("due_at")))
                                and now - d < timedelta(days=180)
                                for f in await dex.crm.list_follow_ups(customer_id=c["customer_id"]))
            if not recent_review:
                due = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
                await dex.crm.create_follow_up({**base, "type": "review_request", "due_at": due})
                n += 1
            start = _dt(j.get("start")) or now
            rebook = (start + timedelta(days=int(settings.get("rebook_days", 90)))).replace(
                hour=10, minute=0, second=0, microsecond=0)
            await dex.crm.create_follow_up({**base, "type": "rebook", "due_at": rebook})
            n += 1
        dex.db.kv_set("post_job_done", done[-2000:])
        return n

    async def reconcile_jobs(self) -> int:
        """Back-fill Jobs rows for Dex calendar events whose row failed to save (FR-S5)."""
        dex, now, n = self.dex, clock.now(), 0
        known = {j.get("calendar_event_id") for j in await dex.crm.list_jobs()}
        for b in await dex.calendar.list_dd_events(now - timedelta(days=7), now + timedelta(days=30)):
            if b.event_id not in known and b.private.get("dd_customer_id"):
                jid = await dex.crm.create_job({"customer_id": b.private["dd_customer_id"], "start": b.start,
                                                "end": b.end, "address": b.location or "", "calendar_event_id": b.event_id,
                                                "booked_by": "agent", "notes": f"Back-filled from calendar: {b.summary}"})
                await dex.calendar.patch_event(b.event_id, private={**b.private, "dd_job_id": jid})
                n += 1
        return n

    # ------------------------------------------------------------ approvals
    async def approval_reminders(self) -> None:
        dex, now = self.dex, clock.now()
        for a in dex.db.list_approvals("pending"):
            age = now - a["created_at"]
            p = a["payload"]
            if a["type"] == "reschedule" and p.get("hold_event_id") and age > timedelta(hours=4):
                await dex.calendar.delete_event(p["hold_event_id"])
                p["hold_event_id"] = ""
                dex.db.save_approval(a["approval_id"], a["thread_id"], a["type"], "pending", p)
                await dex.owner.notify(f"⏰ {a['approval_id']}: no answer in 4 hours, so I released the held time slot. "
                                       "Approving now will re-check the calendar.")
                continue
            if a["type"] in ("late_reply", "reply") and a["thread_id"]:
                t = dex.db.get_thread(a["thread_id"])
                state, _ = dm_window_state(t.get("last_inbound_at") if t else None, now)
                if state == "closed" or (a["type"] == "reply" and state != "open"):
                    c = await dex.crm.get_customer(p.get("customer_id") or "")
                    dex.db.save_approval(a["approval_id"], a["thread_id"], a["type"], "expired", p)
                    await dex.crm.update_approval(a["approval_id"], {"status": "expired", "decided_at": now})
                    if c and p.get("draft"):
                        await dex.crm.create_follow_up({"customer_id": c["customer_id"],
                                                        "customer_name": dex.display_name(c), "type": "custom",
                                                        "due_at": now, "sender": "owner", "message": p["draft"],
                                                        "channel": (t or {}).get("channel", ""), "created_by": "agent"})
                    continue
            if 7 <= now.hour < 21 and age > timedelta(hours=2):
                last = a.get("last_ping_at")
                if not last or now - last > timedelta(hours=2):
                    dex.db.set_approval_ping(a["approval_id"])
                    await dex.owner.notify(f"🔔 Still waiting on {a['approval_id']} ({a['type'].replace('_', ' ')}): "
                                           f"{p.get('summary', '')}")

    # ------------------------------------------------------------ summary / housekeeping
    async def daily_summary(self) -> str:
        text = await self.summary_text()
        await self.dex.owner.notify(text, urgent=True)
        return text

    async def summary_text(self) -> str:
        dex, now = self.dex, clock.now()
        today = await jobs_text(dex, now.date())
        due = [f for f in await dex.crm.list_follow_ups(status="Open", sender="owner")
               if (d := _dt(f.get("due_at"))) and d.date() <= now.date()]
        pending = dex.db.list_approvals("pending")
        since = now - timedelta(hours=14)
        new_leads = [c for c in await dex.crm.list_customers() if (d := _dt(c.get("first_contact_at"))) and d >= since]
        booked = sum(1 for c in new_leads if c.get("lead_status") in ("Booked", "Customer"))
        lines = [f"☀️ {today}",
                 f"Send today: {len(due)} msg{'s' if len(due) != 1 else ''} (Follow-ups tab)"
                 + (f" → {dex.cfg.sheet_url}" if dex.cfg.sheet_url and due else ""),
                 "Pending approvals: " + (", ".join(f"{a['approval_id']} {a['type']}" for a in pending) or "none"),
                 f"New leads overnight: {len(new_leads)} ({booked} booked)"]
        return "\n".join(lines)

    async def retention_cleanup(self) -> None:
        self.dex.db.prune_events(timedelta(days=7))
        self.dex.db.prune_history(timedelta(days=90))

    async def health_ping(self) -> None:
        if self.healthcheck_url:
            try:
                async with httpx.AsyncClient(timeout=10) as h:
                    await h.get(self.healthcheck_url)
            except httpx.HTTPError as e:
                log.warning("health ping failed: %s", e)

    async def flush_alerts(self) -> None:
        if hasattr(self.dex.owner, "flush_batch"):
            await self.dex.owner.flush_batch()
