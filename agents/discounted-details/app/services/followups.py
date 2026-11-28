"""Dex's follow-up desk: the one manual follow-up workflow (docs/FOLLOWUPS.md).

Dex owns it: finds due follow-ups, checks the latest conversation, filters and dedupes, drafts, tracks attempts and
keeps the CRM current. Atlas delivers it: one daily report in Telegram (see Atlas.followup_report).
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app import clock

log = logging.getLogger(__name__)

MAX_SALES_ATTEMPTS = 3
# gap (days) before the NEXT sales attempt, after attempt k is confirmed sent: days 2 -> 5 -> 7 after the quote
NEXT_GAP_DAYS = {1: 3, 2: 2}
KEEP_FOLLOWING_TAG = "keep_following_up"  # authorized exception to the 3-attempt limit

CATEGORY_OF = {"quote_follow_up": "sales", "custom": "sales", "sales": "sales", "reminder": "appointment",
               "appointment_check": "appointment", "review_request": "review", "rebook": "retention",
               "check_in": "check_in", "post_job_check_in": "check_in"}
CATEGORY_LABEL = {"appointment": "📅 Appointment check", "sales": "💬 Sales follow-up", "review": "⭐ Review request",
                  "retention": "🔁 Rebook", "check_in": "👋 Check-in", "other": "📝 Follow-up"}
CATEGORY_ORDER = {"appointment": 0, "sales": 1, "check_in": 2, "review": 3, "retention": 4, "other": 5}
PLATFORMS = [("instagram", "📸 Instagram"), ("messenger", "💬 Facebook Messenger"), ("sms", "📱 Text messages"),
             ("whatsapp", "🟢 WhatsApp"), ("other", "📝 Other")]
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}
CLOSED_SALES_STATUSES = {"Booked", "Customer", "Lost", "DNC", "Inactive"}


def category_of(row: dict[str, Any]) -> str:
    return str(row.get("category") or "") or CATEGORY_OF.get(str(row.get("type") or ""), "other")


def platform_of(row: dict[str, Any], customer: dict[str, Any]) -> str:
    ch = str(row.get("channel") or "").lower()
    if ch in ("instagram", "messenger", "sms", "whatsapp"):
        return ch
    if ch in ("text", "phone"):
        return "sms"
    if customer.get("instagram_igsid"):
        return "instagram"
    if customer.get("messenger_psid"):
        return "messenger"
    if customer.get("phone"):
        return "sms"
    return "other"


def _dt(v: Any) -> datetime | None:
    try:
        return clock.parse_iso(str(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


@dataclass
class Item:
    row: dict[str, Any]
    customer: dict[str, Any]
    platform: str
    category: str
    priority: str
    attempt: int
    context: str
    message: str
    link: str = ""
    notes: list[str] = field(default_factory=list)
    number: int = 0

    @property
    def fid(self) -> str:
        return self.row["follow_up_id"]


class FollowUpDesk:
    def __init__(self, dex, conversations=None):
        self.dex = dex
        self.conversations = conversations  # DMPoller (latest_messages); None in setups without DMs

    # ------------------------------------------------------------------ building the list
    async def due_items(self, now: datetime | None = None) -> list[Item]:
        """Manual follow-ups due today or overdue, filtered, checked against the conversation and deduped."""
        dex, now = self.dex, now or clock.now()
        end_of_day = now.replace(hour=23, minute=59, second=59)
        rows = [r for r in await dex.crm.list_follow_ups(status="Open") if (d := _dt(r.get("due_at"))) and d <= end_of_day]
        rows.sort(key=lambda r: (str(r.get("due_at")), str(r.get("follow_up_id"))))
        items: list[Item] = []
        seen: dict[tuple, Item] = {}
        for r in rows:
            c = await dex.crm.get_customer(r.get("customer_id") or "")
            if not c:
                await self._close(r, "Cancelled", "Customer not found")
                continue
            cat = category_of(r)
            if dex.crm.is_dnc(c):
                await self._close(r, "Cancelled", "Customer asked not to be contacted")
                continue
            if cat == "sales" and (c.get("lead_status") in CLOSED_SALES_STATUSES or await self._has_upcoming_job(c)):
                await self._close(r, "Cancelled", f"No longer needed (lead is {c.get('lead_status') or 'booked'})")
                continue
            if r.get("job_id"):
                job = await dex.crm.get_job(r["job_id"])
                if job and job.get("status") in ("Cancelled", "No-show"):
                    await self._close(r, "Cancelled", f"Job {job.get('status', '').lower()}")
                    continue
            # Dex sends these himself (or asks for approval) while the 24-hour window is open
            thread = dex.thread_for(c)
            if r.get("sender") == "agent" and thread and self._window_open(thread, now) and not dex.paused:
                continue
            key = (c["customer_id"], cat, str(r.get("job_id") or "") if cat == "appointment" else "")
            if key in seen:  # duplicate action for the same customer: keep the first, retire the rest
                await self._close(r, "Cancelled", f"Duplicate of {seen[key].fid}")
                continue
            item = await self._item(r, c, cat, now)
            if item is None:
                continue
            seen[key] = item
            items.append(item)
        items.sort(key=lambda i: ([p for p, _ in PLATFORMS].index(i.platform), PRIORITY_RANK[i.priority],
                                  CATEGORY_ORDER.get(i.category, 9), str(i.row.get("due_at"))))
        for n, it in enumerate(items, 1):
            it.number = n
        return items

    async def _item(self, r: dict[str, Any], c: dict[str, Any], cat: str, now: datetime) -> Item | None:
        dex = self.dex
        attempt = _int(r.get("attempt"), 1) if cat == "sales" else 0
        platform = platform_of(r, c)
        notes: list[str] = []
        # Check the latest conversation before asking the owner to send anything
        if platform in ("instagram", "messenger") and self.conversations:
            sender = c.get("instagram_igsid") if platform == "instagram" else c.get("messenger_psid")
            msgs = await self.conversations.latest_messages(platform, str(sender)) if sender else None
            if msgs is None:
                notes.append("⚠️ Couldn't check the latest messages. Look at the chat before sending.")
            elif msgs:
                last = msgs[-1]
                if not last["from_business"]:
                    # they wrote last: not an unanswered follow-up anymore, Dex/owner should answer them instead
                    await self._close(r, "Cancelled", f"Customer replied {clock.label(last['at'])}")
                    await self.customer_replied(c, why=f"replied {clock.label(last['at'])}")
                    return None
                sent_text = str(r.get("message") or "")[:40].strip().lower()
                prev = _dt(r.get("due_at")) or now
                if last["at"] >= prev - timedelta(hours=12) and sent_text and sent_text in last["text"].lower() \
                        and not dex.db.was_sent_by_dex(last["mid"]):
                    notes.append(f"Looks like this was already sent {clock.label(last['at'])}. Tap ✅ Sent if so.")
        elif platform in ("sms", "whatsapp", "other"):
            notes.append("Can't see this conversation. Check it before sending.")
        message = str(r.get("message") or "") or await self._draft(r, c, cat, attempt)
        if message != r.get("message"):
            await dex.crm.update_follow_up(r["follow_up_id"], {"message": message})
        due = _dt(r.get("due_at")) or now
        overdue_days = (now.date() - due.date()).days
        priority = str(r.get("priority") or "") or self._priority(cat, attempt)
        if overdue_days >= 2 and priority == "normal":
            priority = "high"
        context = str(r.get("context") or "") or await self._context(r, c, cat)
        if overdue_days >= 1:
            context += f" Overdue {overdue_days} day{'s' if overdue_days != 1 else ''}."
        return Item(row=r, customer=c, platform=platform, category=cat, priority=priority, attempt=attempt,
                    context=context.strip(), message=message, link=self._link(platform, c, message), notes=notes)

    @staticmethod
    def _priority(cat: str, attempt: int) -> str:
        if cat == "appointment":
            return "high"
        if cat == "sales":
            return "high" if attempt <= 1 else "normal"
        return "low"

    async def _context(self, r: dict[str, Any], c: dict[str, Any], cat: str) -> str:
        dex = self.dex
        if cat == "sales":
            q = dex.db.kv_get(f"last_quote:{c['customer_id']}", {}) or {}
            what = " ".join(x for x in (q.get("service_name"), f"for their {q['vehicle']}" if q.get("vehicle") else "")
                            if x)
            price = f" at ${q['total']}" if q.get("total") else ""
            return f"Quoted {what or 'a detail'}{price}; no reply since." if q or what else \
                "Asked about a detail and went quiet."
        if r.get("job_id"):
            job = await dex.crm.get_job(r["job_id"]) or {}
            start = _dt(job.get("start"))
            when = clock.label(start) if start else "their appointment"
            if cat == "appointment":
                return f"Booked for {when}; confirm they're still on."
            if cat == "review":
                return f"Detailed on {when.split(' at ')[0]}; ask for a review."
            if cat == "retention":
                return f"Last detail {when.split(' at ')[0]}; time to rebook."
            if cat == "check_in":
                return f"Detailed on {when.split(' at ')[0]}; check how it's holding up."
        return str(r.get("notes") or "").split(";")[0][:120] or "Follow-up due."

    async def _draft(self, r: dict[str, Any], c: dict[str, Any], cat: str, attempt: int) -> str:
        from app.jobs.tasks import quote_followup_text, render
        if cat == "sales":
            return quote_followup_text(self.dex, c, {1: 2, 2: 5}.get(attempt, 7))
        job = await self.dex.crm.get_job(r["job_id"]) if r.get("job_id") else None
        return await render(self.dex, str(r.get("type") or ""), c, job) or "Hi! Just checking in from Discounted Details."

    @staticmethod
    def _link(platform: str, c: dict[str, Any], message: str) -> str:
        if platform == "instagram" and c.get("instagram_username"):
            return f"https://ig.me/m/{c['instagram_username']}"
        if platform == "messenger":
            return "https://business.facebook.com/latest/inbox/messenger"
        if platform == "whatsapp" and c.get("phone"):
            digits = re.sub(r"\D", "", str(c["phone"]))
            return f"https://wa.me/{digits}?text={urllib.parse.quote(message)}"
        return ""

    def _window_open(self, thread: dict[str, Any], now: datetime) -> bool:
        from app.services.channels import dm_window_state
        state, _ = dm_window_state(thread.get("last_inbound_at"), now)
        return state == "open" and not self.dex.is_paused(thread)

    async def _has_upcoming_job(self, c: dict[str, Any]) -> bool:
        now = clock.now()
        return any(j.get("status") == "Booked" and (s := _dt(j.get("start"))) and s >= now
                   for j in await self.dex.crm.list_jobs(c["customer_id"]))

    async def _close(self, r: dict[str, Any], status: str, why: str) -> None:
        old = str(r.get("notes") or "")
        await self.dex.crm.update_follow_up(r["follow_up_id"], {"status": status,
                                                                "notes": (f"{old}; {why}" if old else why)[-500:]})

    # ------------------------------------------------------------------ completion
    async def complete(self, fid: str, action: str = "sent", *, by: str = "owner") -> str:
        """Owner confirmed (sent) or skipped one follow-up. Idempotent: repeats never double-count."""
        dex, now = self.dex, clock.now()
        rows = [r for r in await dex.crm.list_follow_ups() if r.get("follow_up_id") == fid]
        if not rows:
            return f"{fid} not found."
        r = rows[0]
        if r.get("status") != "Open":
            when = f" {r['sent_at']}" if r.get("sent_at") else ""
            return f"{fid} was already marked {str(r.get('status')).lower()}{when}."
        c = await dex.crm.get_customer(r.get("customer_id") or "") or {}
        name = dex.display_name(c) if c else fid
        cat = category_of(r)
        if action == "skip":
            await dex.crm.update_follow_up(fid, {"status": "Skipped", "completed_by": by, "category": cat})
            self._forget(fid)
            return f"⏭ {name}: skipped."
        attempt = _int(r.get("attempt"), 1) if cat == "sales" else 0
        await dex.crm.update_follow_up(fid, {"status": "Sent", "sent_at": now, "completed_by": by, "category": cat,
                                             **({"attempt": attempt} if cat == "sales" else {})})
        await dex.crm.log_message({"customer_id": c.get("customer_id", ""), "channel": r.get("channel", ""),
                                   "sent_via": "owner", "direction": "out", "text": r.get("message", ""),
                                   "message_id": f"manual:{fid}"})
        self._forget(fid)
        dex.activity.log("Dex", f"You sent {name} their {CATEGORY_LABEL.get(cat, 'follow-up')[2:].lower()}"
                                + (f" ({attempt}/{MAX_SALES_ATTEMPTS})" if cat == "sales" else ""), "Follow-up sent ✅")
        if cat != "sales" or not c:
            return f"✅ {name}: marked sent."
        # sales: schedule the next attempt, or stop after the third
        for other in await dex.crm.list_follow_ups(status="Open", customer_id=c["customer_id"]):
            if category_of(other) == "sales":  # old pre-scheduled attempts would double up
                await self._close(other, "Cancelled", f"Replaced by the next attempt after {fid}")
        tags = str(c.get("tags") or "").lower()
        if attempt >= MAX_SALES_ATTEMPTS and KEEP_FOLLOWING_TAG not in tags:
            await dex.crm.update_customer(c["customer_id"], {"lead_status": "Inactive"})
            await dex.crm.append_customer_note(c["customer_id"],
                                               f"{now:%m/%d} {MAX_SALES_ATTEMPTS} follow-ups sent with no reply; "
                                               "marked inactive.")
            return f"✅ {name}: marked sent. That was follow-up {attempt}/{MAX_SALES_ATTEMPTS}, so they're now inactive."
        nxt = attempt + 1
        due = (now + timedelta(days=NEXT_GAP_DAYS.get(attempt, 2))).replace(hour=10, minute=0, second=0, microsecond=0)
        from app.jobs.tasks import quote_followup_text
        await dex.crm.create_follow_up({
            "customer_id": c["customer_id"], "customer_name": name, "type": r.get("type") or "quote_follow_up",
            "due_at": due, "sender": "owner", "channel": r.get("channel", ""), "created_by": "dex",
            "message": quote_followup_text(dex, c, {2: 5}.get(nxt, 7)), "attempt": nxt, "category": "sales",
            "notes": f"Follow-up {nxt}/{MAX_SALES_ATTEMPTS} after {fid}"})
        return f"✅ {name}: marked sent ({attempt}/{MAX_SALES_ATTEMPTS}). Next one {clock.label(due).split(' at ')[0]}."

    async def complete_many(self, numbers: list[int] | str, action: str = "sent") -> str:
        """By report number from the latest report ('all' = everything still pending in it)."""
        report = self.dex.db.kv_get("followup_report") or {}
        pending: dict[str, str] = report.get("items", {})
        if not pending:
            return "There's nothing pending in today's follow-up list."
        nums = sorted(pending, key=int) if numbers == "all" else [str(n) for n in numbers]
        out, missing = [], [n for n in nums if n not in pending]
        for n in nums:
            if n in pending:
                out.append(f"{n}. " + await self.complete(pending[n], action))
        if missing:
            out.append(f"Not in today's list (or already done): {', '.join(missing)}")
        left = len((self.dex.db.kv_get("followup_report") or {}).get("items", {}))
        out.append(f"{left} left." if left else "🎉 All caught up.")
        return "\n".join(out)

    def _forget(self, fid: str) -> None:
        report = self.dex.db.kv_get("followup_report") or {}
        items = {n: f for n, f in report.get("items", {}).items() if f != fid}
        if report:
            self.dex.db.kv_set("followup_report", {**report, "items": items})

    # ------------------------------------------------------------------ customer replies
    async def customer_replied(self, c: dict[str, Any], why: str = "Customer replied") -> int:
        """A reply ends the unanswered-sales sequence and reactivates an inactive lead. Retention, review and
        appointment actions stay (they're not about the reply)."""
        n = 0
        for f in await self.dex.crm.list_follow_ups(status="Open", customer_id=c["customer_id"]):
            if category_of(f) == "sales":
                await self._close(f, "Cancelled", why)
                self._forget(f["follow_up_id"])
                n += 1
        if c.get("lead_status") == "Inactive":
            await self.dex.crm.update_customer(c["customer_id"], {"lead_status": "Quoted"})
            c["lead_status"] = "Quoted"
        return n

    # ------------------------------------------------------------------ the report
    async def send_report(self, header: str = "🧭 Follow-ups") -> int:
        """One organized list in Telegram: summary, then each platform, then a numbered card per customer."""
        dex, now = self.dex, clock.now()
        items = await self.due_items(now)
        dex.db.kv_set("followup_report", {"date": now.date().isoformat(),
                                          "items": {str(i.number): i.fid for i in items}})
        if not items:
            await dex.owner.notify(f"{header} · {now:%a, %b} {now.day}\n✅ You're all caught up. No follow-ups to send.",
                                   urgent=True)
            return 0
        counts = " · ".join(f"{label.split(' ', 1)[1]} {sum(1 for i in items if i.platform == p)}"
                            for p, label in PLATFORMS if any(i.platform == p for i in items))
        await dex.owner.notify(
            f"{header} · {now:%a, %b} {now.day}\n{len(items)} to send: {counts}\n"
            "Copy each message, send it, then tap ✅ Sent. Or reply /done 1 3 (or /done all).", urgent=True)
        for p, label in PLATFORMS:
            group = [i for i in items if i.platform == p]
            if not group:
                continue
            await dex.owner.notify(f"{label} ({len(group)})", urgent=True)
            for it in group:
                await self._send_card(it)
        if len(items) > 1:
            await dex.owner.post_buttons(f"Sent them all? ({len(items)})", [[(f"✅ I sent all {len(items)}", "FU:all")]])
        dex.activity.log("Atlas", f"Sent your follow-up list: {len(items)} to send", f"{len(items)} follow-ups to send")
        return len(items)

    async def _send_card(self, it: Item) -> None:
        c = it.customer
        who = self.dex.display_name(c)
        handle = f" (@{c['instagram_username']})" if it.platform == "instagram" and c.get("instagram_username") \
            and c.get("instagram_username") != who else ""
        phone = f" · {c['phone']}" if it.platform in ("sms", "whatsapp") and c.get("phone") else ""
        prio = {"high": " · 🔥 High", "normal": "", "low": ""}[it.priority]
        kind = CATEGORY_LABEL.get(it.category, "📝 Follow-up")
        if it.category == "sales":
            kind += f" {it.attempt}/{MAX_SALES_ATTEMPTS}"
        lines = [f"<b>{it.number}. {html.escape(who)}{html.escape(handle)}</b>{html.escape(phone)}{prio}",
                 f"{kind}",
                 html.escape(it.context),
                 "",
                 f"<code>{html.escape(it.message)}</code>"]
        lines += [html.escape(n) for n in it.notes]
        buttons: list[list[dict[str, Any]]] = []
        row1 = []
        if len(it.message) <= 256:
            row1.append({"text": "📋 Copy message", "copy_text": {"text": it.message}})
        if it.link:
            row1.append({"text": "💬 Open chat", "url": it.link})
        if row1:
            buttons.append(row1)
        buttons.append([{"text": "✅ Sent", "callback_data": f"{it.fid}:sent"},
                        {"text": "⏭ Skip", "callback_data": f"{it.fid}:skip"}])
        await self.dex.owner.send_card("\n".join(lines), buttons)
