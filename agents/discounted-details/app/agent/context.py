"""Per-turn context block (PRD §7.4). Customer text is escaped so it can't close or open our tags."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import yaml

from app import clock
from app.services.channels import dm_window_state

KEEP_SETTINGS = ("business_name", "work_start", "work_end", "min_notice_hours", "travel_free_miles",
                 "travel_fee_per_mile", "facebook_review_url", "payment_methods", "booking_prep_message",
                 "rebook_days")


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _y(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()


def conversation_state(thread: dict[str, Any] | None, customer: dict[str, Any] | None, now: datetime,
                       dnc: bool) -> dict[str, Any]:
    if not thread:
        return {"channel": None, "dm_window": "closed", "paused": False, "do_not_contact": dnc}
    state, left = dm_window_state(thread.get("last_inbound_at"), now)
    out: dict[str, Any] = {"channel": thread.get("channel"), "dm_window": state}
    if state == "open":
        out["minutes_left"] = int(left)
    elif state == "human_agent":
        out["hours_left"] = round(left, 1)
    out["paused"] = bool(thread.get("paused_until") and thread["paused_until"] > now)
    out["do_not_contact"] = dnc
    return out


def compact_customer(c: dict[str, Any] | None, vehicles: list[dict[str, Any]]) -> Any:
    if not c:
        return "new"
    keep = ("customer_id", "first_name", "last_name", "phone", "email", "address", "lead_status", "tags", "notes",
            "total_jobs", "last_job_date")
    out = {k: c.get(k) for k in keep if c.get(k) not in (None, "")}
    if vehicles:
        out["vehicles"] = [{k: v.get(k) for k in ("vehicle_id", "year", "make", "model", "color", "size_class")
                            if v.get(k) not in (None, "")} for v in vehicles]
    return out


def services_block(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in ("service_key", "name", "size_class", "price", "duration_min", "includes")}
            for r in rows if str(r.get("active", True)).upper() not in ("FALSE", "0")]


def build_turn(*, mode: str, now: datetime, thread: dict[str, Any] | None, customer: dict[str, Any] | None,
               vehicles: list[dict[str, Any]], jobs: list[dict[str, Any]], follow_ups: list[dict[str, Any]],
               approvals: list[dict[str, Any]], services: list[dict[str, Any]], settings: dict[str, Any],
               dnc: bool, body_tag: str, body: str) -> str:
    upcoming = sorted([j for j in jobs if j.get("status") == "Booked"], key=lambda j: str(j.get("start")))
    past = sorted([j for j in jobs if j.get("status") != "Booked"], key=lambda j: str(j.get("start")))[-5:]
    job_keys = ("job_id", "service", "addons", "condition", "start", "address", "total", "status")
    parts = [
        f"<now>{clock.to_local(now).strftime('%a, %b %-d, %Y %-I:%M %p')} Central</now>",
        f"<mode>{mode}</mode>",
        f"<conversation_state>\n{_y(conversation_state(thread, customer, now, dnc))}\n</conversation_state>",
        f"<customer>\n{esc(_y(compact_customer(customer, vehicles)))}\n</customer>",
        f"<jobs>\n{esc(_y({'upcoming': [{k: j.get(k) for k in job_keys} for j in upcoming], 'past': [{k: j.get(k) for k in job_keys} for j in past]}))}\n</jobs>",
        f"<open_follow_ups>\n{esc(_y([{k: f.get(k) for k in ('follow_up_id', 'type', 'due_at', 'sender')} for f in follow_ups]))}\n</open_follow_ups>",
        f"<pending_approvals>\n{esc(_y([{'approval_id': a['approval_id'], 'type': a['type']} for a in approvals]))}\n</pending_approvals>",
        f"<services>\n{esc(_y(services_block(services)))}\n</services>",
        f"<business_settings>\n{esc(_y({k: settings.get(k) for k in KEEP_SETTINGS}))}\n</business_settings>",
        f"<{body_tag}>\n{esc(body)}\n</{body_tag}>",
    ]
    return "\n".join(parts)
