"""Tool dispatcher (PRD Step 13). Converts our exceptions into the error codes in PRD §7.5."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app import clock
from app.agent.tools import MODES
from app.core import Dex, ToolError
from app.guardrails import COMPLAINT_HOLDING_MESSAGE, TurnState
from app.services.maps import AddressNotFound, check_service_area
from app.services.scheduling import SlotError, find_slots
from app.services.sheets import UpstreamError

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    dex: Dex
    mode: str
    customer: dict[str, Any] | None = None
    thread: dict[str, Any] | None = None
    state: TurnState = field(default_factory=TurnState)
    turn_id: str = ""
    owner_replies: list[str] = field(default_factory=list)
    kind: str = "reply"  # 'proactive' in scheduled tasks


NUMERIC_NONE = {"standard_total"}


def _blank_to_none(key: str, value: Any) -> Any:
    """Tools use "" / 0 for 'unknown' (strict schemas can't be nullable); handlers expect None."""
    if isinstance(value, str) and not value.strip():
        return None
    if key in NUMERIC_NONE and value == 0:
        return None
    if isinstance(value, dict) and key == "vehicle":
        v = {k: (None if (isinstance(x, str) and not x.strip()) or (k == "year" and x == 0) else x)
             for k, x in value.items()}
        return v if v.get("make") or v.get("model") else None
    return value


def _date(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


async def run_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> tuple[str, bool]:
    if ctx.mode not in MODES.get(name, set()):
        return json.dumps({"error": "TOOL_NOT_ALLOWED_IN_MODE", "detail": f"{name} not allowed in {ctx.mode}"}), True
    try:
        fn = HANDLERS[name]
    except KeyError:
        return json.dumps({"error": "UNKNOWN_TOOL"}), True
    params = [p for p in inspect.signature(fn).parameters if p != "ctx"]
    args = {k: _blank_to_none(k, v) for k, v in (args or {}).items() if k in params}  # ignore unknown fields
    for p in params:
        args.setdefault(p, None)  # schemas aren't strict, so tolerate omitted fields
    for k in ("addons", "tags_add", "recipient_customer_ids"):
        if k in args and args[k] is None:
            args[k] = []
    try:
        result = await fn(ctx, **args)
        return json.dumps(result, default=str), False
    except ToolError as e:
        return json.dumps({"error": e.code, "detail": e.detail}), True
    except SlotError as e:
        return json.dumps({"error": e.code, "detail": e.detail}), True
    except AddressNotFound as e:
        return json.dumps({"error": "ADDRESS_NOT_FOUND", "detail": str(e)}), True
    except UpstreamError as e:
        return json.dumps({"error": "UPSTREAM_ERROR", "detail": str(e)}), True
    except TypeError as e:  # malformed arguments
        return json.dumps({"error": "INVALID_ARGUMENTS", "detail": str(e)}), True
    except Exception as e:
        log.exception("tool %s crashed", name)
        return json.dumps({"error": "UPSTREAM_ERROR", "detail": type(e).__name__}), True


def _need_customer(ctx: ToolContext) -> dict[str, Any]:
    if not ctx.customer:
        raise ToolError("NO_CUSTOMER", "No current customer in this mode; pass customer_id.")
    return ctx.customer


async def quote_price(ctx, service, size_class, addons, condition, address):
    q = await ctx.dex.quote(service, size_class, addons, condition, address)
    ctx.state.verified_amounts |= q.amounts()
    if ctx.mode == "CUSTOMER_CONVERSATION" and ctx.customer and ctx.thread:
        if ctx.customer.get("lead_status") in ("", "New", None, "Lost"):
            await ctx.dex.crm.update_customer(ctx.customer["customer_id"], {"lead_status": "Quoted"})
            ctx.customer["lead_status"] = "Quoted"
        ctx.dex.db.upsert_thread(ctx.thread["thread_id"], last_quote_at=clock.now())
        vehicles = await ctx.dex.crm.list_vehicles(ctx.customer["customer_id"])
        car = " ".join(str(v.get(k) or "") for v in vehicles[-1:] for k in ("make", "model")).strip()
        ctx.dex.db.kv_set(f"last_quote:{ctx.customer['customer_id']}",
                          {"service_name": q.service_name, "total": q.total, "vehicle": car})
        if not q.requires_approval:
            ctx.dex.activity.log("Dex", f"Quoted ${q.total} to {ctx.dex.display_name(ctx.customer)}: "
                                        f"{q.service_name}{' for the ' + car if car else ''}",
                                 f"Quoted ${q.total}")
    return q.to_dict()


async def check_area(ctx, address):
    res = await check_service_area(ctx.dex.maps, address, await ctx.dex.crm.get_settings())
    if res["travel_fee"]:
        ctx.state.verified_amounts.add(res["travel_fee"])
    return res


async def find_available_slots(ctx, service, size_class, addons, condition, address, earliest_date, latest_date,
                               time_of_day, max_results):
    q = await ctx.dex.quote(service, size_class, addons, condition, address)
    ctx.state.verified_amounts |= q.amounts()
    try:
        slots = await find_slots(calendar=ctx.dex.calendar, maps=ctx.dex.maps, settings=await ctx.dex.crm.get_settings(),
                                 signing_key=ctx.dex.cfg.signing_key, duration_min=q.duration_min, address=address,
                                 now=clock.now(), earliest_date=_date(earliest_date), latest_date=_date(latest_date),
                                 time_of_day=time_of_day, max_results=max(1, min(5, int(max_results or 3))))
    except UpstreamError as e:
        raise ToolError("UPSTREAM_ERROR", str(e)) from e
    if not slots:
        return {"slots": [], "note": "NO_AVAILABILITY in that range; try a wider range or another time of day."}
    return {"slots": [s.to_dict() for s in slots], "duration_min": q.duration_min, "total": q.total}


async def book_appointment(ctx, slot_id, customer_name, phone, address, vehicle, service, addons, condition, notes):
    customer = _need_customer(ctx)
    override = None
    for a in ctx.dex.db.list_approvals():
        p = a["payload"]
        if p.get("customer_id") == customer["customer_id"] and a["type"] in ("discount", "custom_quote") \
                and a["status"] == "approved" and p.get("override_price") is not None:
            override = int(p["override_price"])
    return await ctx.dex.book(customer, slot_id=slot_id, customer_name=customer_name, phone=phone, address=address,
                              vehicle=vehicle, service=service, addons=addons, condition=condition, notes=notes or "",
                              state=ctx.state, override_price=override,
                              source_channel=(ctx.thread or {}).get("channel", ""))


async def update_customer(ctx, first_name, last_name, phone, email, address, notes_append, tags_add, vehicle):
    c = _need_customer(ctx)
    fields = {k: v for k, v in {"first_name": first_name, "last_name": last_name, "phone": phone, "email": email,
                                "address": address}.items() if v}
    if fields:
        await ctx.dex.crm.update_customer(c["customer_id"], fields)
        c.update(fields)
    if notes_append:
        await ctx.dex.crm.append_customer_note(c["customer_id"], notes_append)
    if tags_add:
        await ctx.dex.crm.add_tags(c["customer_id"], tags_add)
    if vehicle:
        await ctx.dex.crm.upsert_vehicle(c["customer_id"], vehicle)
    return {"customer_id": c["customer_id"]}


async def _customer_by_id(ctx, customer_id):
    if customer_id:
        c = await ctx.dex.crm.get_customer(customer_id)
        if not c:
            raise ToolError("CUSTOMER_NOT_FOUND", customer_id)
        return c
    return _need_customer(ctx)


async def create_follow_up(ctx, customer_id, type, due_iso, sender, message, job_id):
    c = await _customer_by_id(ctx, customer_id)
    if ctx.dex.crm.is_dnc(c):
        raise ToolError("DO_NOT_CONTACT")
    thread = ctx.dex.thread_for(c)
    fid = await ctx.dex.crm.create_follow_up({
        "customer_id": c["customer_id"], "customer_name": ctx.dex.display_name(c), "job_id": job_id or "",
        "type": type, "due_at": clock.parse_iso(due_iso), "sender": sender,
        "channel": (thread or {}).get("channel", ""), "message": message, "created_by": "agent"})
    return {"follow_up_id": fid}


async def request_owner_approval(ctx, type, summary, customer_request, job_id, slot_id, address, standard_total,
                                 draft_customer_message, recipient_customer_ids):
    dex = ctx.dex
    customer = ctx.customer
    payload: dict[str, Any] = {}
    hold = ""
    if standard_total:
        payload["standard_total"] = standard_total
    if type == "reschedule":
        if not (slot_id and address and job_id):
            raise ToolError("INVALID_ARGUMENTS", "reschedule needs job_id, slot_id and address")
        hold_info = await dex.place_hold(_need_customer(ctx), slot_id, address, "pending")
        hold = hold_info["hold_event_id"]
        payload.update(hold_info)
        summary = f"{summary} → {clock.label(clock.parse_iso(hold_info['new_start']))}"
    if type == "bulk_message":
        payload["recipient_ids"] = recipient_customer_ids
        summary = f"{summary} ({len(recipient_customer_ids)} customers)"
        customer = None
    if type == "complaint":
        ctx.state.complaint_mode = True
    aid = await dex.create_approval(type, customer, summary=summary, customer_request=customer_request,
                                    draft=draft_customer_message, job_id=job_id or "", payload=payload,
                                    hold_event_id=hold)
    return {"approval_id": aid, "status": "pending",
            **({"note": f"Send exactly this holding message: {COMPLAINT_HOLDING_MESSAGE}"} if type == "complaint" else {})}


async def escalate_to_owner(ctx, reason, question, urgency):
    aid = await ctx.dex.create_approval("question", ctx.customer, summary=reason.replace("_", " "),
                                        customer_request=question)
    return {"approval_id": aid}


async def mark_do_not_contact(ctx, reason):
    await ctx.dex.mark_dnc(_need_customer(ctx), reason)
    return {"ok": True}


async def send_reply(ctx, text, customer_id):
    c = await _customer_by_id(ctx, customer_id)
    return await ctx.dex.send_reply(c, text, ctx.state, kind=ctx.kind, turn_id=ctx.turn_id)


async def reply_to_owner(ctx, text):
    ctx.owner_replies.append(text)
    await ctx.dex.owner.notify(text, urgent=True)
    return {"ok": True}


async def search_customers(ctx, query, tags_any, vehicle_make, size_class, last_job_before, last_job_after, limit):
    crm = ctx.dex.crm
    rows = [c for c in await crm.list_customers() if not crm.is_dnc(c)]
    vehicles: dict[str, list[dict[str, Any]]] = {}
    if vehicle_make or size_class:
        for v in await crm._read("Vehicles"):
            vehicles.setdefault(v["customer_id"], []).append(v)
    out = []
    for c in rows:
        blob = " ".join(str(c.get(k, "")) for k in ("first_name", "last_name", "notes", "tags", "address")).lower()
        if query and query.lower() not in blob:
            continue
        tags = {t.strip().lower() for t in str(c.get("tags", "")).split(",")}
        if tags_any and not tags & {t.lower() for t in tags_any}:
            continue
        vs = vehicles.get(c["customer_id"], [])
        if vehicle_make and not any(vehicle_make.lower() in str(v.get("make", "")).lower() for v in vs):
            continue
        if size_class and not any(v.get("size_class") == size_class for v in vs):
            continue
        lj = str(c.get("last_job_date") or "")
        if last_job_before and (not lj or lj[:10] >= last_job_before):
            continue
        if last_job_after and (not lj or lj[:10] <= last_job_after):
            continue
        t = ctx.dex.thread_for(c)
        out.append({"customer_id": c["customer_id"], "name": ctx.dex.display_name(c),
                    "channel": (t or {}).get("channel"), "last_job": lj,
                    "window_open": bool(t and t.get("last_inbound_at")
                                        and clock.now() - t["last_inbound_at"] < timedelta(hours=23, minutes=45))})
    return {"count": len(out), "customers": out[:max(1, min(500, int(limit or 50)))]}


async def get_schedule(ctx, date_from, date_to):
    lo, hi = date.fromisoformat(date_from), date.fromisoformat(date_to)
    jobs = []
    for j in await ctx.dex.crm.list_jobs():
        try:
            d = datetime.fromisoformat(str(j["start"])).date()
        except ValueError:
            continue
        if lo <= d <= hi and j.get("status") in ("Booked", "Completed"):
            c = await ctx.dex.crm.get_customer(j["customer_id"])
            jobs.append({"job_id": j["job_id"], "start": j["start"], "customer": ctx.dex.display_name(c),
                         "service": j["service"], "address": j["address"], "total": j["total"], "status": j["status"]})
    return {"jobs": sorted(jobs, key=lambda x: str(x["start"]))}


async def list_follow_ups(ctx, status, sender):
    rows = await ctx.dex.crm.list_follow_ups(status=status, sender=sender)
    return {"follow_ups": [{k: r.get(k) for k in ("follow_up_id", "customer_name", "type", "due_at", "sender",
                                                  "status", "message")} for r in rows][:50]}


HANDLERS = {
    "quote_price": quote_price, "check_service_area": check_area, "find_available_slots": find_available_slots,
    "book_appointment": book_appointment, "update_customer": update_customer, "create_follow_up": create_follow_up,
    "request_owner_approval": request_owner_approval, "escalate_to_owner": escalate_to_owner,
    "mark_do_not_contact": mark_do_not_contact, "send_reply": send_reply, "reply_to_owner": reply_to_owner,
    "search_customers": search_customers, "get_schedule": get_schedule, "list_follow_ups": list_follow_ups,
}
