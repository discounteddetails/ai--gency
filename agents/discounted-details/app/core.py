"""Dex's runtime: inbound handling, safe sending, approvals and booking. Everything the LLM tools and the
scheduled jobs call goes through here so the hard rules are enforced in one place."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app import clock
from app.db import DB
from app.guardrails import DNC_CLOSING_MESSAGE, GuardrailBlocked, TurnState, check_outgoing, is_hard_opt_out
from app.services import pricing
from app.services.calendar import Calendar
from app.services.channels import choose_channel, dm_window_state
from app.services.composio_client import ComposioError, WindowClosedError
from app.services.debounce import Debouncer
from app.services.dm_client import DMClient
from app.services.maps import AddressNotFound, Maps
from app.services.owner_telegram import OwnerChannel
from app.services.scheduling import BOOKING_LOCK, SlotError, recheck_slot, verify_slot
from app.services.sheets import SheetsCRM, UpstreamError

if TYPE_CHECKING:
    from app.agent.runner import AgentRunner
    from app.jobs.dm_poller import InboundEvent

log = logging.getLogger(__name__)
HOLD_HOURS = 4
CHANNEL_LABEL = {"instagram": "Instagram", "messenger": "Messenger"}


class ToolError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class DexConfig:
    signing_key: str
    owner_takeover_hours: int = 12
    debounce_seconds: float = 20
    shadow_default: bool = True
    sheet_url: str = ""


class Dex:
    def __init__(self, *, cfg: DexConfig, db: DB, crm: SheetsCRM, calendar: Calendar, maps: Maps, dm: DMClient,
                 owner: OwnerChannel):
        self.cfg, self.db, self.crm, self.calendar, self.maps, self.dm, self.owner = cfg, db, crm, calendar, maps, dm, owner
        self.runner: AgentRunner | None = None
        self.debouncer = Debouncer(cfg.debounce_seconds, self._process_thread)
        self._thread_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ flags
    @property
    def paused(self) -> bool:
        return bool(self.db.kv_get("global_pause", False))

    @property
    def shadow(self) -> bool:
        return bool(self.db.kv_get("shadow_mode", self.cfg.shadow_default))

    def thread_lock(self, thread_id: str) -> asyncio.Lock:
        return self._thread_locks.setdefault(thread_id, asyncio.Lock())

    # ------------------------------------------------------------------ customers / threads
    async def customer_for_thread(self, channel: str, sender_id: str, name_hint: str = "") -> dict[str, Any]:
        c = await self.crm.find_customer(channel, sender_id)
        if c:
            return c
        col = {"messenger": "messenger_psid", "instagram": "instagram_igsid"}[channel]
        fields: dict[str, Any] = {col: sender_id, "preferred_channel": channel}
        if name_hint:
            if channel == "instagram" and " " not in name_hint:
                fields["instagram_username"] = name_hint
            else:
                parts = name_hint.split()
                fields["first_name"], fields["last_name"] = parts[0], " ".join(parts[1:])
        cid = await self.crm.create_customer(fields)
        return await self.crm.get_customer(cid) or {"customer_id": cid, **fields}

    def thread_for(self, customer: dict[str, Any]) -> dict[str, Any] | None:
        for channel, col in (("instagram", "instagram_igsid"), ("messenger", "messenger_psid")):
            if customer.get(col):
                t = self.db.get_thread(f"{channel}:{customer[col]}")
                if t:
                    return t
        for channel, col in (("instagram", "instagram_igsid"), ("messenger", "messenger_psid")):
            if customer.get(col):
                return {"thread_id": f"{channel}:{customer[col]}", "channel": channel, "sender_id": customer[col],
                        "last_inbound_at": None, "paused_until": None}
        return None

    def is_paused(self, thread: dict[str, Any] | None) -> bool:
        return bool(thread and thread.get("paused_until") and thread["paused_until"] > clock.now())

    # ------------------------------------------------------------------ inbound
    async def on_customer_message(self, ev: InboundEvent) -> None:
        customer = await self.customer_for_thread(ev.channel, ev.sender_id, ev.sender_name)
        self.db.upsert_thread(ev.thread_id, channel=ev.channel, sender_id=ev.sender_id,
                              customer_id=customer["customer_id"], last_inbound_at=ev.timestamp)
        text = ev.text or ("[customer sent an attachment]" if ev.attachments else "")
        await self.crm.log_message({"customer_id": customer["customer_id"], "channel": ev.channel, "direction": "in",
                                    "text": text, "attachment_url": ", ".join(ev.attachments),
                                    "message_id": ev.mid})
        await self.crm.update_customer(customer["customer_id"], {"last_inbound_at": ev.timestamp})
        who = self.display_name(customer)
        if self.crm.is_dnc(customer):
            await self.owner.notify(f"🚫 Do-Not-Contact customer {who} ({CHANNEL_LABEL[ev.channel]}) messaged again: "
                                    f"\"{text[:200]}\". Dex didn't reply.")
            return
        if is_hard_opt_out(text):
            await self.mark_dnc(customer, "Replied: " + text[:60])
            try:
                await self._dm_send(customer, ev.channel, ev.sender_id, DNC_CLOSING_MESSAGE)
            except ComposioError:
                pass
            return
        if self.paused:
            await self.owner.notify(f"⏸ New DM from {who} ({CHANNEL_LABEL[ev.channel]}): \"{text[:300]}\" "
                                    "(Dex is paused)")
            return
        self.debouncer.add(ev.thread_id, ev)

    async def on_owner_message(self, ev: InboundEvent) -> None:
        until = clock.now() + timedelta(hours=self.cfg.owner_takeover_hours)
        self.db.upsert_thread(ev.thread_id, channel=ev.channel, sender_id=ev.sender_id, paused_until=until)
        customer = await self.crm.find_customer(ev.channel, ev.sender_id)
        await self.crm.log_message({"customer_id": (customer or {}).get("customer_id", ""), "channel": ev.channel,
                                    "direction": "owner_manual", "text": ev.text, "message_id": ev.mid})
        log.info("owner took over %s until %s", ev.thread_id, until)

    async def _process_thread(self, thread_id: str, events: list[InboundEvent]) -> None:
        if self.is_paused(self.db.get_thread(thread_id)) or not self.runner:
            return
        await self.runner.run_customer_turn(thread_id, events)

    # ------------------------------------------------------------------ outbound
    @staticmethod
    def display_name(customer: dict[str, Any] | None) -> str:
        if not customer:
            return "Unknown"
        name = " ".join(x for x in (str(customer.get("first_name") or ""),
                                    str(customer.get("last_name") or "")[:1]) if x).strip()
        return name or str(customer.get("instagram_username") or customer.get("customer_id"))

    async def _dm_send(self, customer: dict[str, Any], channel: str, recipient: str, text: str, *,
                       human_agent: bool = False, turn_id: str = "") -> str:
        mid = await self.dm.send_text(channel, recipient, text, human_agent=human_agent, _approved_path=human_agent)
        await self.crm.log_message({"customer_id": customer.get("customer_id", ""), "channel": channel,
                                    "sent_via": "human_agent" if human_agent else "response", "direction": "out",
                                    "text": text, "message_id": mid, "agent_turn_id": turn_id})
        return mid

    async def send_reply(self, customer: dict[str, Any], text: str, state: TurnState, *, kind: str = "reply",
                         turn_id: str = "") -> dict[str, Any]:
        """The only path from the LLM to a customer. Raises ToolError with the codes in PRD §7.5."""
        if self.crm.is_dnc(customer):
            raise ToolError("DO_NOT_CONTACT")
        thread = self.thread_for(customer)
        if not thread:
            raise ToolError("WINDOW_CLOSED", "No Instagram/Messenger conversation with this customer.")
        if self.is_paused(thread) or self.paused:
            raise ToolError("THREAD_PAUSED")
        try:
            check_outgoing(text, state)
        except GuardrailBlocked as e:
            raise ToolError("GUARDRAIL_BLOCKED", e.reason) from e
        settings = await self.crm.get_settings()
        route = choose_channel(thread["channel"], thread.get("last_inbound_at"), kind, clock.now(), settings)
        if route == "owner":
            raise ToolError("WINDOW_CLOSED", "No allowed channel right now; create an owner follow-up.")
        state.sends += 1
        if state.shadow or self.shadow:
            aid = await self.create_approval("reply", customer, summary=f"Reply to {self.display_name(customer)}",
                                             draft=text, payload={"human_agent": route == "human_agent"})
            return {"status": "PENDING_OWNER_APPROVAL", "approval_id": aid}
        if route == "human_agent":
            aid = await self.create_approval("late_reply", customer, summary="Late reply", draft=text,
                                             payload={"human_agent": True})
            return {"status": "PENDING_OWNER_APPROVAL", "approval_id": aid}
        try:
            mid = await self._dm_send(customer, thread["channel"], thread["sender_id"], text, turn_id=turn_id)
        except WindowClosedError as e:
            raise ToolError("WINDOW_CLOSED", str(e)) from e
        except ComposioError as e:
            raise ToolError("UPSTREAM_ERROR", str(e)) from e
        return {"message_id": mid}

    async def deliver_owner_text(self, customer: dict[str, Any], text: str) -> str:
        """Send text the owner wrote or approved: DM if open, Human Agent if allowed, else owner follow-up."""
        thread = self.thread_for(customer)
        if not thread or self.crm.is_dnc(customer):
            return "skipped"
        settings = await self.crm.get_settings()
        route = choose_channel(thread["channel"], thread.get("last_inbound_at"), "reply", clock.now(), settings)
        try:
            if route in ("dm", "human_agent"):
                await self._dm_send(customer, thread["channel"], thread["sender_id"], text,
                                    human_agent=route == "human_agent")
                return route
        except ComposioError as e:
            log.warning("owner-approved send failed, falling back to follow-up: %s", e)
        await self.crm.create_follow_up({"customer_id": customer["customer_id"],
                                         "customer_name": self.display_name(customer), "type": "custom",
                                         "due_at": clock.now(), "sender": "owner", "channel": thread["channel"],
                                         "message": text, "created_by": "agent"})
        return "owner"

    async def mark_dnc(self, customer: dict[str, Any], reason: str) -> None:
        cid = customer["customer_id"]
        await self.crm.update_customer(cid, {"do_not_contact": True, "dnc_reason": reason, "lead_status": "DNC"})
        for fu in await self.crm.list_follow_ups(status="Open", customer_id=cid):
            await self.crm.update_follow_up(fu["follow_up_id"], {"status": "Cancelled"})

    # ------------------------------------------------------------------ approvals
    BUTTONS = {
        "discount": [[("✅ Approve", "approve"), ("❌ Decline", "decline")], [("💲 Set price", "price"),
                                                                           ("✏️ Edit reply", "edit")]],
        "custom_quote": [[("✅ Approve", "approve"), ("❌ Decline", "decline")], [("💲 Set price", "price"),
                                                                               ("✏️ Edit reply", "edit")]],
        "cancel": [[("✅ Approve", "approve"), ("❌ Decline", "decline"), ("✏️ Edit reply", "edit")]],
        "reschedule": [[("✅ Approve", "approve"), ("❌ Decline", "decline"), ("✏️ Edit reply", "edit")]],
        "complaint": [[("✅ Send draft", "approve"), ("✏️ Edit reply", "edit")], [("🙋 I'll reply myself", "self")]],
        "bulk_message": [[("✅ Approve", "approve"), ("❌ Decline", "decline"), ("✏️ Edit", "edit")]],
        "reply": [[("✅ Send", "approve"), ("✏️ Edit", "edit"), ("❌ Skip", "decline")]],
        "late_reply": [[("✅ Send", "approve"), ("✏️ Edit", "edit"), ("❌ Skip", "decline")]],
        "question": [[("💬 Answer", "answer"), ("🙋 I'll reply myself", "self")]],
    }
    ICON = {"discount": "💲", "custom_quote": "📐", "cancel": "🛑", "reschedule": "🔁", "complaint": "⚠️",
            "bulk_message": "📣", "reply": "✉️", "late_reply": "🕑", "question": "❓"}

    async def create_approval(self, type_: str, customer: dict[str, Any] | None, *, summary: str,
                              customer_request: str = "", draft: str = "", job_id: str = "",
                              payload: dict[str, Any] | None = None, hold_event_id: str = "") -> str:
        payload = dict(payload or {})
        aid = await self.crm.create_approval({
            "type": type_, "customer_id": (customer or {}).get("customer_id", ""), "job_id": job_id,
            "summary": summary[:200], "customer_request": customer_request, "draft_customer_message": draft,
            "proposed": payload, "hold_event_id": hold_event_id})
        thread = self.thread_for(customer) if customer else None
        payload.update({"draft": draft, "customer_id": (customer or {}).get("customer_id"), "job_id": job_id,
                        "hold_event_id": hold_event_id, "summary": summary, "customer_request": customer_request})
        who = self.display_name(customer) if customer else "Customers"
        ch = f" ({CHANNEL_LABEL.get(thread['channel'], '')})" if thread else ""
        lines = [f"{self.ICON.get(type_, '🔔')} {aid} · {type_.replace('_', ' ').upper()}", f"{who}{ch}: {summary}"]
        if customer_request:
            lines.append(f"They said: \"{customer_request[:300]}\"")
        if draft:
            lines.append(f"Draft reply: \"{draft}\"")
        buttons = [[(t, f"{aid}:{a}") for t, a in row] for row in self.BUTTONS.get(type_, self.BUTTONS["reply"])]
        msg_id = await self.owner.post_buttons("\n".join(lines), buttons)
        self.db.save_approval(aid, thread["thread_id"] if thread else None, type_, "pending", payload, msg_id)
        return aid

    async def decide(self, approval_id: str, decision: str, *, text: str | None = None,
                     price: int | None = None) -> str:
        """decision: approve | decline | edit | price | answer | self. Returns a short status for the owner."""
        appr = self.db.get_approval(approval_id)
        if not appr:
            return f"{approval_id} not found."
        if appr["status"] != "pending":
            return f"{approval_id} was already {appr['status']}."
        type_, p = appr["type"], appr["payload"]
        customer = await self.crm.get_customer(p.get("customer_id") or "") if p.get("customer_id") else None
        status = {"approve": "approved", "decline": "denied", "edit": "edited", "price": "approved",
                  "answer": "approved", "self": "denied"}[decision]
        if decision == "price" and price is not None:
            p["override_price"] = int(price)
        self.db.save_approval(approval_id, appr["thread_id"], type_, status, p)
        await self.crm.update_approval(approval_id, {"status": status, "decided_at": clock.now(),
                                                     "owner_response": text or decision,
                                                     "override_price": p.get("override_price", "")})
        if appr.get("telegram_message_id"):
            await self.owner.mark_decided(appr["telegram_message_id"],
                                          f"{approval_id} · {type_} → {status.upper()} at "
                                          f"{clock.now().strftime('%-I:%M %p')}")
        try:
            result = await self._apply(type_, decision, p, customer, approval_id, text, appr["thread_id"])
        except Exception as e:
            log.exception("applying %s failed", approval_id)
            await self.owner.notify(f"⚠️ Couldn't finish {approval_id}: {e}", urgent=True)
            return f"{approval_id} saved, but the follow-through failed: {e}"
        return result

    async def _apply(self, type_: str, decision: str, p: dict[str, Any], customer: dict[str, Any] | None,
                     aid: str, text: str | None, thread_id: str | None) -> str:
        if type_ in ("reply", "late_reply"):
            if decision in ("approve", "edit") and customer:
                route = await self.deliver_owner_text(customer, text if decision == "edit" else p.get("draft", ""))
                return f"{aid}: {'sent' if route != 'owner' else 'window closed, added to your follow-ups'}."
            return f"{aid}: skipped."
        if type_ == "complaint":
            if decision in ("approve", "edit") and customer:
                await self.deliver_owner_text(customer, text if decision == "edit" else p.get("draft", ""))
                return f"{aid}: reply sent."
            return f"{aid}: OK, you'll reply yourself."
        if type_ == "bulk_message":
            if decision not in ("approve", "edit"):
                return f"{aid}: promo cancelled."
            return await self._send_bulk(p, text if decision == "edit" else p.get("draft", ""))
        if type_ == "question" and decision == "self":
            return f"{aid}: OK, you'll answer them yourself."
        if type_ == "cancel" and decision == "approve":
            await self._cancel_job(p.get("job_id", ""))
        if type_ == "reschedule":
            if p.get("hold_event_id"):
                await self.calendar.delete_event(p["hold_event_id"])
            if decision == "approve":
                await self._move_job(p.get("job_id", ""), clock.parse_iso(p["new_start"]),
                                     clock.parse_iso(p["new_end"]))
        if decision == "edit" and customer:
            await self.deliver_owner_text(customer, text or "")
            return f"{aid}: done, your message was sent."
        if customer and thread_id and self.runner:
            await self.runner.run_approval_result(thread_id, aid, type_,
                                                  "denied" if decision == "decline" else "approved", p, text)
        return f"{aid}: done."

    async def _send_bulk(self, p: dict[str, Any], text: str) -> str:
        sent = queued = 0
        for cid in p.get("recipient_ids", []):
            c = await self.crm.get_customer(cid)
            if not c or self.crm.is_dnc(c):
                continue
            thread = self.thread_for(c)
            window, _ = dm_window_state(thread.get("last_inbound_at") if thread else None, clock.now())
            if thread and window == "open":
                try:
                    await self._dm_send(c, thread["channel"], thread["sender_id"], text)
                    sent += 1
                    continue
                except ComposioError:
                    pass
            await self.crm.create_follow_up({"customer_id": cid, "customer_name": self.display_name(c),
                                             "type": "custom", "due_at": clock.now(), "sender": "owner",
                                             "channel": (thread or {}).get("channel", ""), "message": text,
                                             "created_by": "agent", "notes": "Promo"})
            queued += 1
        return f"Promo: {sent} sent by DM, {queued} added to your follow-up list."

    async def _cancel_job(self, job_id: str) -> None:
        job = await self.crm.get_job(job_id)
        if not job:
            return
        if job.get("calendar_event_id"):
            await self.calendar.delete_event(job["calendar_event_id"])
        await self.crm.update_job(job_id, {"status": "Cancelled"})
        for fu in await self.crm.list_follow_ups(status="Open", customer_id=job["customer_id"]):
            if fu.get("job_id") == job_id:
                await self.crm.update_follow_up(fu["follow_up_id"], {"status": "Cancelled"})

    async def _move_job(self, job_id: str, start: datetime, end: datetime) -> None:
        job = await self.crm.get_job(job_id)
        if not job:
            return
        if job.get("calendar_event_id"):
            await self.calendar.patch_event(job["calendar_event_id"], start=start, end=end)
        await self.crm.update_job(job_id, {"start": start, "end": end})
        for fu in await self.crm.list_follow_ups(status="Open", customer_id=job["customer_id"]):
            if fu.get("job_id") == job_id and fu.get("type") == "reminder":
                await self.crm.update_follow_up(fu["follow_up_id"], {"due_at": reminder_due(start)})

    async def place_hold(self, customer: dict[str, Any], slot_id: str, address: str, aid_hint: str) -> dict[str, Any]:
        start, dur = verify_slot(self.cfg.signing_key, slot_id, address, clock.now())
        end = start + timedelta(minutes=dur)
        eid = await self.calendar.create_event(start, end, f"HOLD (pending {aid_hint}): {self.display_name(customer)}",
                                               address, "Reschedule awaiting owner approval", "8",
                                               {"dd_hold_approval_id": aid_hint})
        return {"hold_event_id": eid, "new_start": start.isoformat(), "new_end": end.isoformat()}

    # ------------------------------------------------------------------ pricing / booking
    async def quote(self, service: str, size_class: str, addons: list[str], condition: str,
                    address: str | None) -> pricing.Quote:
        settings = await self.crm.get_settings()
        miles = None
        if address:
            try:
                formatted = await self.maps.geocode(address)
                miles = (await self.maps.drive(str(settings["service_area_origin"]), formatted)).miles
            except AddressNotFound as e:
                raise ToolError("ADDRESS_NOT_FOUND", str(e)) from e
            except UpstreamError as e:
                raise ToolError("UPSTREAM_ERROR", str(e)) from e
        try:
            return pricing.quote(service, size_class, addons, condition, miles, await self.crm.get_services(),
                                 settings)
        except pricing.PricingError as e:
            raise ToolError(e.code, e.detail) from e

    async def book(self, customer: dict[str, Any], *, slot_id: str, customer_name: str, phone: str | None,
                   address: str, vehicle: dict[str, Any], service: str, addons: list[str], condition: str,
                   notes: str, state: TurnState, override_price: int | None = None,
                   source_channel: str = "") -> dict[str, Any]:
        """FR-S5: verify -> lock -> recheck -> event -> job row -> customer/vehicle -> reminder."""
        try:
            start, dur = verify_slot(self.cfg.signing_key, slot_id, address, clock.now())
        except SlotError as e:
            raise ToolError(e.code, e.detail) from e
        q = await self.quote(service, vehicle.get("size_class", "sedan"), addons, condition, address)
        if q.requires_approval and override_price is None:
            raise ToolError("PRICE_NEEDS_APPROVAL", q.approval_reason or "")
        if dur < q.duration_min:
            raise ToolError("SLOT_EXPIRED", "Job is longer than the offered slot; find slots again.")
        settings = await self.crm.get_settings()
        end = start + timedelta(minutes=q.duration_min)
        total = override_price if override_price is not None else q.total
        async with BOOKING_LOCK:
            try:
                free = await recheck_slot(calendar=self.calendar, maps=self.maps, settings=settings, start=start,
                                          duration_min=q.duration_min, address=address)
            except UpstreamError as e:
                raise ToolError("UPSTREAM_ERROR", str(e)) from e
            if not free:
                raise ToolError("SLOT_TAKEN")
            cid = customer["customer_id"]
            first, *rest = (customer_name or "").split()
            await self.crm.update_customer(cid, {k: v for k, v in {
                "first_name": first or customer.get("first_name"), "last_name": " ".join(rest) or None,
                "phone": phone, "address": address, "lead_status": "Booked"}.items() if v})
            vid = await self.crm.upsert_vehicle(cid, vehicle)
            label = {"exterior": "Exterior Detail", "interior": "Interior Detail", "full": "Full Detail"}[service]
            title = f"DD: {label} – {customer_name or self.display_name(customer)} ({vehicle.get('size_class', '')})"
            desc = "\n".join([f"{q.service_name}{' + ' + ', '.join(a['name'] for a in q.addons) if q.addons else ''}",
                              f"Condition: {condition}", f"Vehicle: {vehicle.get('year', '')} {vehicle.get('make', '')} "
                              f"{vehicle.get('model', '')}", f"Total: ${total}", f"Phone: {phone or '-'}",
                              f"Customer: {cid}", f"Notes: {notes or '-'}", self.cfg.sheet_url])
            try:
                event_id = await self.calendar.create_event(start, end, title, address, desc, "9",
                                                            {"dd_job_id": "pending", "dd_customer_id": cid})
            except UpstreamError as e:
                raise ToolError("UPSTREAM_ERROR", str(e)) from e
        job_fields = {"customer_id": cid, "vehicle_id": vid, "service": service, "addons": addons,
                      "condition": condition, "size_class": vehicle.get("size_class", ""), "address": address,
                      "start": start, "end": end, "base_price": q.base,
                      "addons_price": sum(a["price"] for a in q.addons), "condition_price": q.condition_surcharge,
                      "travel_fee": q.travel_fee, "override_price": override_price if override_price is not None else "",
                      "total": total, "calendar_event_id": event_id, "booked_by": "agent",
                      "source_channel": source_channel, "notes": notes}
        job_id = ""
        for attempt in range(3):
            try:
                job_id = await self.crm.create_job(job_fields)
                break
            except UpstreamError:
                await asyncio.sleep(2 ** attempt)
        if job_id:
            await self.calendar.patch_event(event_id, private={"dd_job_id": job_id, "dd_customer_id": cid})
            await self.crm.create_follow_up({"customer_id": cid, "customer_name": customer_name, "job_id": job_id,
                                             "type": "reminder", "due_at": reminder_due(start), "sender": "agent",
                                             "channel": source_channel, "message": "", "created_by": "scheduler"})
        else:
            await self.owner.notify(f"⚠️ Booked {customer_name} for {clock.label(start)} on your calendar, but the "
                                    "Jobs row failed to save. It will be back-filled tonight.", urgent=True)
        state.booking_succeeded = True
        state.verified_amounts |= q.amounts() | {total}
        return {"job_id": job_id, "calendar_event_id": event_id, "start_label": clock.label(start),
                "total": total, "duration_min": q.duration_min}


def reminder_due(start: datetime) -> datetime:
    d = clock.to_local(start) - timedelta(days=1)
    return d.replace(hour=9, minute=0, second=0, microsecond=0)
