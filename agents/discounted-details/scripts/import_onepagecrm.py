"""Import a OnePageCRM contacts CSV into the Customers tab (PRD Step 21).

Usage: uv run python scripts/import_onepagecrm.py contacts.csv [--dry-run]
Skips duplicates by phone or email. Channel IDs get linked later when the customer messages (phone match).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.sheets import normalize_phone  # noqa: E402

COLS = {"first_name": ["First name", "First Name", "first_name"], "last_name": ["Last name", "Last Name", "last_name"],
        "phone": ["Phone", "Phones", "Mobile", "phone"], "email": ["Email", "Emails", "email"],
        "address": ["Address", "Street", "address"], "tags": ["Tags", "tags"], "notes": ["Notes", "Background", "notes"]}


def pick(row: dict[str, str], keys: list[str]) -> str:
    return next((row[k].strip() for k in keys if row.get(k)), "")


def rows_from_csv(path: str) -> list[dict[str, str]]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = {k: pick(row, v) for k, v in COLS.items()}
            r["phone"] = normalize_phone(r["phone"].split(",")[0])
            r["email"] = r["email"].split(",")[0].strip().lower()
            text = (r["tags"] + " " + r["notes"]).lower()
            r["lead_status"] = "Customer" if any(w in text for w in ("detail", "job", "paid", "customer")) else "New"
            if r["first_name"] or r["phone"] or r["email"]:
                out.append(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rows = rows_from_csv(a.csv)
    if a.dry_run:
        print(f"{len(rows)} contacts parsed (dry run). First 3: {rows[:3]}")
        return
    import asyncio

    from app.services.sheets import GspreadStore, SheetsCRM

    crm = SheetsCRM(GspreadStore(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ["GOOGLE_SHEET_ID"]))

    async def run():
        existing = await crm.list_customers()
        phones = {normalize_phone(c.get("phone")) for c in existing} - {""}
        emails = {str(c.get("email", "")).lower() for c in existing} - {""}
        added = skipped = 0
        for r in rows:
            if (r["phone"] and r["phone"] in phones) or (r["email"] and r["email"] in emails):
                skipped += 1
                continue
            await crm.create_customer({k: v for k, v in r.items() if v})
            phones.add(r["phone"])
            emails.add(r["email"])
            added += 1
        print(json.dumps({"added": added, "skipped_duplicates": skipped}))

    asyncio.run(run())


if __name__ == "__main__":
    main()
