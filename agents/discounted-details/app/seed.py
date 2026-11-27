"""Seed rows and tab headers (PRD §6.2, §10.1). Used by scripts/bootstrap_sheet.py and tests only.
Runtime prices always come from the live Services tab."""

from __future__ import annotations

TABS: dict[str, list[str]] = {
    "Follow-ups": ["follow_up_id", "customer_id", "customer_name", "job_id", "type", "due_at", "sender", "channel",
                   "message", "status", "created_by", "sent_at", "notes"],
    "Jobs": ["job_id", "customer_id", "vehicle_id", "service", "addons", "condition", "size_class", "address", "start",
             "end", "base_price", "addons_price", "condition_price", "travel_fee", "override_price", "total", "status",
             "completed_by", "calendar_event_id", "booked_by", "source_channel", "notes", "photos_status",
             "created_at", "updated_at"],
    "Customers": ["customer_id", "first_name", "last_name", "phone", "email", "address", "messenger_psid",
                  "instagram_igsid", "instagram_username", "preferred_channel", "lead_status", "do_not_contact",
                  "dnc_reason", "tags", "notes", "first_contact_at", "last_inbound_at", "last_job_date", "total_jobs",
                  "lifetime_value", "created_at", "updated_at"],
    "Vehicles": ["vehicle_id", "customer_id", "year", "make", "model", "color", "size_class", "notes", "created_at"],
    "Approvals": ["approval_id", "created_at", "type", "customer_id", "job_id", "summary", "customer_request",
                  "draft_customer_message", "proposed", "status", "decided_at", "owner_response", "override_price",
                  "hold_event_id"],
    "Messages": ["ts", "customer_id", "channel", "sent_via", "direction", "text", "attachment_url", "message_id",
                 "agent_turn_id"],
    "Services": ["service_key", "name", "size_class", "price", "duration_min", "includes", "active"],
    "Settings": ["key", "value", "description"],
    "Content": ["content_id", "created_at", "kind", "status", "scheduled_for", "caption", "platforms", "ig_media_id",
                "fb_post_id", "notes"],
    "Activity": ["ts", "agent", "text", "short"],
}

ID_PREFIX = {"Customers": ("customer_id", "C"), "Vehicles": ("vehicle_id", "V"), "Jobs": ("job_id", "J"),
             "Follow-ups": ("follow_up_id", "F"), "Approvals": ("approval_id", "A"),
             "Content": ("content_id", "N")}

_EXT = ("Full exterior contact wash; wheel and rim cleaning; tire and wheel well cleaning; protective wax sealant; "
        "tire dressing; exterior window and mirror cleaning; full exterior hand drying.")
_INT = ("Full interior blowout and vacuum; carpet and upholstery shampoo; cloth seat shampoo and extraction; leather "
        "seat cleaning and conditioning; steam cleaning; floor mat cleaning and dressing; dashboard, door panels and "
        "center console cleaning; all interior surfaces cleaned; interior window and mirror cleaning.")
_FULL = ("Everything in the Interior Detail plus everything in the Exterior Detail: complete interior and exterior "
         "cleaning and protection.")

_PRICES = {
    "exterior": ("Exterior Detail", 90, _EXT, {"sedan": 75, "small_suv_truck": 85, "large_suv": 95, "van": 105}),
    "interior": ("Interior Detail", 180, _INT, {"sedan": 165, "small_suv_truck": 185, "large_suv": 205, "van": 225}),
    "full": ("Full Detail", 240, _FULL, {"sedan": 225, "small_suv_truck": 255, "large_suv": 285, "van": 315}),
}

SERVICES_ROWS: list[dict] = [
    {"service_key": key, "name": name, "size_class": sc, "price": price, "duration_min": dur, "includes": inc,
     "active": True}
    for key, (name, dur, inc, prices) in _PRICES.items() for sc, price in prices.items()
] + [
    {"service_key": "addon_pet_hair", "name": "Pet hair removal", "size_class": "any", "price": 25, "duration_min": 30,
     "includes": "Interior and Full Details only.", "active": True},
    {"service_key": "addon_stains", "name": "Stain removal", "size_class": "any", "price": 35, "duration_min": 30,
     "includes": "Interior and Full Details only. Results depend on the stain; some set-in stains can only be "
                 "lightened.", "active": True},
    {"service_key": "condition_worse", "name": "Condition: worse than normal", "size_class": "any", "price": 20,
     "duration_min": 30, "includes": "Interior and Full Details only. Noticeable buildup inside.", "active": True},
    {"service_key": "condition_bad", "name": "Condition: bad", "size_class": "any", "price": 45, "duration_min": 60,
     "includes": "Interior and Full Details only. Heavy dirt inside.", "active": True},
    {"service_key": "condition_terrible", "name": "Condition: terrible", "size_class": "any", "price": 70,
     "duration_min": 90, "includes": "Interior and Full Details only. Extreme inside. Mold/biohazards are custom "
                                     "quotes.", "active": True},
]

SETTINGS_ROWS: list[tuple[str, object, str]] = [
    ("business_name", "Discounted Details", "Name Dex uses with customers"),
    ("timezone", "America/Chicago", "All times are Central"),
    ("work_start", "07:00", "Earliest job start"),
    ("work_end", "20:00", "Jobs must finish by this time"),
    ("min_notice_hours", 3, "Minimum hours between booking and appointment start"),
    ("booking_horizon_days", 21, "How far ahead Dex offers times"),
    ("slot_grid_minutes", 30, "Start times on :00 and :30"),
    ("buffer_minutes", 15, "Extra time added to drive time before and after each job"),
    ("travel_free_miles", 25, "Free driving miles from Kyle"),
    ("travel_fee_per_mile", 2, "Dollars per extra one-way mile"),
    ("custom_quote_over_miles", 60, "Beyond this, Dex asks you first"),
    ("service_area_origin", "Kyle, TX 78640", "Where distance is measured from"),
    ("home_base_address", "Kyle, TX 78640", "Where your day starts (for the first job's drive time)"),
    ("rebook_days", 90, "Days after a job to suggest rebooking"),
    ("facebook_review_url", "https://www.facebook.com/profile.php?id=61584982506383&sk=reviews", "Review link"),
    ("payment_methods", "Cash, Cash App, Venmo, Apple Pay", "What you accept"),
    ("booking_prep_message", "", "Optional note added to booking confirmations"),
    ("owner_takeover_hours", 12, "How long Dex stays quiet after you reply to a customer yourself"),
    ("extra_busy_calendar_ids", "", "Other calendars that block time (Todoist is ignored)"),
    ("human_agent_messenger", False, "Set TRUE after the 7-day reply test passes on Messenger"),
    ("human_agent_instagram", False, "Set TRUE after the 7-day reply test passes on Instagram"),
    ("promo_keyword", "", "Comment keyword (e.g. FRIDAY). Anyone who comments it gets promo_dm as a DM. Blank = off"),
    ("promo_dm", "", "The DM sent to people who comment the keyword"),
    ("promo_details", "", "The offer Dex honors without asking you (exact terms)"),
    ("promo_ends", "", "Last day of the offer (YYYY-MM-DD). After this, Dex stops honoring it"),
]

DEFAULT_SETTINGS: dict[str, object] = {k: v for k, v, _ in SETTINGS_ROWS}
