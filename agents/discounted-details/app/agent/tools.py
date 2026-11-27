"""Tool definitions for Claude (PRD §7.5). Strict schemas with no optional or union-typed fields; order and content are deterministic so the prompt
cache prefix stays stable."""

from __future__ import annotations

from typing import Any

SERVICE = {"type": "string", "enum": ["exterior", "interior", "full"]}
SIZE = {"type": "string", "enum": ["sedan", "small_suv_truck", "large_suv", "van"]}
ADDONS = {"type": "array", "items": {"type": "string", "enum": ["pet_hair", "stains"]}}
CONDITION = {"type": "string", "enum": ["standard", "worse", "bad", "terrible"],
             "description": "Interior/Full only; use standard for Exterior."}
S = {"type": "string"}
# No nullable/union types: the strict-tool grammar allows only a few. "Unknown" is "" (strings) or 0 (integers).
S_NULL = {"type": "string", "description": "Empty string if unknown or not applicable."}
I_NULL = {"type": "integer", "description": "0 if unknown or not applicable."}

VEHICLE = {"type": "object", "additionalProperties": False,
           "properties": {"year": I_NULL, "make": S, "model": S, "color": S_NULL, "size_class": SIZE},
           "required": ["year", "make", "model", "color", "size_class"]}


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "additionalProperties": False, "properties": props,
                             "required": list(props)}}


TOOLS: list[dict[str, Any]] = [
    _tool("quote_price",
          "Calculate the exact price, travel fee and job length. Call before stating any total. Prices come only "
          "from this tool. Pass the address (ZIP/city is fine) to include the travel fee, or an empty string without it.",
          {"service": SERVICE, "size_class": SIZE, "addons": ADDONS, "condition": CONDITION, "address": S_NULL}),
    _tool("check_service_area",
          "Driving distance from Kyle, TX and travel fee for an address. Never estimate distance yourself.",
          {"address": S}),
    _tool("find_available_slots",
          "Open appointment times on the owner's calendar for this job, drive time included. Offer ONLY slots "
          "returned here. Dates are YYYY-MM-DD or empty strings.",
          {"service": SERVICE, "size_class": SIZE, "addons": ADDONS, "condition": CONDITION, "address": S,
           "earliest_date": S_NULL, "latest_date": S_NULL,
           "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening", "any"]},
           "max_results": {"type": "integer", "description": "1 to 5"}}),
    _tool("book_appointment",
          "Book a slot from find_available_slots. Re-checks the calendar. Only after success may you say the "
          "booking is confirmed. Use the same address the slot was found for.",
          {"slot_id": S, "customer_name": S, "phone": S_NULL, "address": S, "vehicle": VEHICLE,
           "service": SERVICE, "addons": ADDONS, "condition": CONDITION, "notes": S_NULL}),
    _tool("update_customer",
          "Save what you learn about the current customer right away (name, phone, email, address, notes, tags, "
          "vehicle). Use empty strings for anything unknown, and a vehicle with empty make if there is none.",
          {"first_name": S_NULL, "last_name": S_NULL, "phone": S_NULL, "email": S_NULL, "address": S_NULL,
           "notes_append": S_NULL, "tags_add": {"type": "array", "items": S},
           "vehicle": VEHICLE}),
    _tool("create_follow_up",
          "Add a to-do with a complete ready-to-send message. sender 'owner' when no channel is open. "
          "Never for Do Not Contact customers. due_iso is an ISO datetime.",
          {"customer_id": S_NULL,
           "type": {"type": "string", "enum": ["reminder", "quote_follow_up", "review_request", "rebook", "custom"]},
           "due_iso": S, "sender": {"type": "string", "enum": ["agent", "owner"]}, "message": S, "job_id": S_NULL}),
    _tool("request_owner_approval",
          "Ask the owner on Telegram before: discounts/custom quotes, cancelling, rescheduling (pass slot_id and "
          "address of the new time), complaint replies, or messages to a group (pass recipient_customer_ids).",
          {"type": {"type": "string",
                    "enum": ["discount", "custom_quote", "cancel", "reschedule", "complaint", "bulk_message"]},
           "summary": S, "customer_request": S, "job_id": S_NULL, "slot_id": S_NULL, "address": S_NULL,
           "standard_total": I_NULL, "draft_customer_message": S,
           "recipient_customer_ids": {"type": "array", "items": S}}),
    _tool("escalate_to_owner",
          "Alert the owner about something you can't answer or a system problem. No approval needed.",
          {"reason": {"type": "string", "enum": ["unknown_question", "upstream_error", "unclear", "other"]},
           "question": S, "urgency": {"type": "string", "enum": ["normal", "high"]}}),
    _tool("mark_do_not_contact",
          "The customer said stop / not interested / don't message me. Stops all future messages and follow-ups.",
          {"reason": S}),
    _tool("send_reply",
          "The ONLY way to message a customer; text outside this tool is never delivered. Replies on the "
          "customer's channel. customer_id is an empty string for the current customer (required in scheduled tasks).",
          {"text": S, "customer_id": S_NULL}),
    _tool("reply_to_owner", "Owner command mode only: reply to the owner in Telegram. Keep it short.",
          {"text": S}),
    _tool("search_customers",
          "Owner command mode: find customers (Do Not Contact customers are always excluded). Dates YYYY-MM-DD.",
          {"query": S_NULL, "tags_any": {"type": "array", "items": S}, "vehicle_make": S_NULL,
           "size_class": S_NULL, "last_job_before": S_NULL, "last_job_after": S_NULL,
           "limit": {"type": "integer", "description": "1 to 500"}}),
    _tool("get_schedule", "Owner command mode: booked jobs between two dates (YYYY-MM-DD, inclusive).",
          {"date_from": S, "date_to": S}),
    _tool("list_follow_ups", "Owner command mode: follow-ups filtered by status and sender (empty string = any).",
          {"status": S_NULL, "sender": S_NULL}),
]

C, A, S_, OW = "CUSTOMER_CONVERSATION", "APPROVAL_RESULT", "SCHEDULED_TASK", "OWNER_COMMAND"
MODES: dict[str, set[str]] = {
    "quote_price": {C, A, S_, OW}, "check_service_area": {C, OW}, "find_available_slots": {C, A, OW},
    "book_appointment": {C, A, OW}, "update_customer": {C, A, OW}, "create_follow_up": {C, A, S_, OW},
    "request_owner_approval": {C, OW}, "escalate_to_owner": {C, A, S_, OW}, "mark_do_not_contact": {C},
    "send_reply": {C, A, S_}, "reply_to_owner": {OW}, "search_customers": {OW}, "get_schedule": {OW},
    "list_follow_ups": {OW},
}
