"""Google Sheets CRM (PRD §10.1). A small table store abstraction keeps the CRM logic testable without Google."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any, Protocol

from app import clock
from app.seed import DEFAULT_SETTINGS, ID_PREFIX, TABS


class UpstreamError(Exception):
    """A Google/Composio/Maps call failed after retries."""


class TableStore(Protocol):
    def read_all(self, tab: str) -> list[dict[str, Any]]: ...
    def append(self, tab: str, row: dict[str, Any]) -> None: ...
    def update(self, tab: str, key_col: str, key: str, fields: dict[str, Any]) -> bool: ...


class MemoryStore:
    """In-memory TableStore for tests and local dry runs."""

    def __init__(self, tabs: dict[str, list[str]] | None = None):
        self.headers = dict(tabs or TABS)
        self.rows: dict[str, list[dict[str, Any]]] = {t: [] for t in self.headers}

    def read_all(self, tab: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows[tab]]

    def append(self, tab: str, row: dict[str, Any]) -> None:
        self.rows[tab].append({h: row.get(h, "") for h in self.headers[tab]})

    def update(self, tab: str, key_col: str, key: str, fields: dict[str, Any]) -> bool:
        for r in self.rows[tab]:
            if str(r.get(key_col)) == str(key):
                r.update({k: v for k, v in fields.items() if k in self.headers[tab]})
                return True
        return False


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (list, dict)):
        return ", ".join(map(str, v)) if isinstance(v, list) else json.dumps(v)
    if hasattr(v, "isoformat"):
        return clock.to_local(v).strftime("%Y-%m-%d %H:%M")
    return v


class GspreadStore:
    """TableStore backed by a real spreadsheet via gspread (service account)."""

    def __init__(self, service_account_json: str, sheet_id: str):
        import gspread  # local import keeps tests free of Google deps

        gc = gspread.service_account_from_dict(json.loads(service_account_json))
        self._ss = gc.open_by_key(sheet_id)
        self._ws: dict[str, Any] = {}

    def _sheet(self, tab: str):
        if tab not in self._ws:
            self._ws[tab] = self._ss.worksheet(tab)
        return self._ws[tab]

    def _retry(self, fn):
        delay = 1.0
        for attempt in range(4):
            try:
                return fn()
            except Exception as e:  # gspread.exceptions.APIError and transport errors
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt == 3 or (status is not None and status < 500 and status != 429):
                    raise UpstreamError(f"Google Sheets: {e}") from e
                time.sleep(delay + random.random() * 0.2)
                delay *= 2

    def read_all(self, tab: str) -> list[dict[str, Any]]:
        return self._retry(lambda: self._sheet(tab).get_all_records(default_blank=""))

    def append(self, tab: str, row: dict[str, Any]) -> None:
        headers = TABS[tab]
        self._retry(lambda: self._sheet(tab).append_row([_cell(row.get(h)) for h in headers],
                                                        value_input_option="USER_ENTERED"))

    def update(self, tab: str, key_col: str, key: str, fields: dict[str, Any]) -> bool:
        ws = self._sheet(tab)
        headers = self._retry(lambda: ws.row_values(1))
        col = headers.index(key_col) + 1
        keys = self._retry(lambda: ws.col_values(col))
        try:
            row_idx = keys.index(str(key)) + 1
        except ValueError:
            return False
        updates = [{"range": f"{_a1(headers.index(k) + 1)}{row_idx}", "values": [[_cell(v)]]}
                   for k, v in fields.items() if k in headers]
        if updates:
            self._retry(lambda: ws.batch_update(updates, value_input_option="USER_ENTERED"))
        return True


def _a1(col: int) -> str:
    s = ""
    while col:
        col, r = divmod(col - 1, 26)
        s = chr(65 + r) + s
    return s


def normalize_phone(raw: str | None) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ("+" + digits) if digits else ""


def _truthy(v: Any) -> bool:
    return str(v).strip().upper() in ("TRUE", "1", "YES", "Y")


class SheetsCRM:
    """Async CRM API over a TableStore. All Google I/O runs in a worker thread."""

    CACHE_SECONDS = 300

    def __init__(self, store: TableStore):
        self.store = store
        self._cache: dict[str, tuple[float, Any]] = {}
        self._id_lock = asyncio.Lock()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def _read(self, tab: str) -> list[dict[str, Any]]:
        return await self._run(self.store.read_all, tab)

    async def _next_id(self, tab: str) -> str:
        col, prefix = ID_PREFIX[tab]
        rows = await self._read(tab)
        nums = [int(m.group(1)) for r in rows if (m := re.fullmatch(prefix + r"(\d+)", str(r.get(col, ""))))]
        return f"{prefix}{(max(nums) + 1 if nums else 1):04d}"

    async def _create(self, tab: str, row: dict[str, Any]) -> str:
        col, _ = ID_PREFIX[tab]
        async with self._id_lock:
            new_id = await self._next_id(tab)
            row = {**row, col: new_id}
            await self._run(self.store.append, tab, {k: _cell(v) for k, v in row.items()})
        return new_id

    async def _update(self, tab: str, key: str, fields: dict[str, Any]) -> bool:
        col = ID_PREFIX[tab][0] if tab in ID_PREFIX else "key"
        return await self._run(self.store.update, tab, col, key, {k: _cell(v) for k, v in fields.items()})

    # ---------- reference data ----------
    async def get_services(self) -> list[dict[str, Any]]:
        hit = self._cache.get("services")
        if hit and time.monotonic() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        rows = [r for r in await self._read("Services") if r.get("service_key")]
        self._cache["services"] = (time.monotonic(), rows)
        return rows

    async def get_settings(self) -> dict[str, Any]:
        hit = self._cache.get("settings")
        if hit and time.monotonic() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        out = dict(DEFAULT_SETTINGS)
        for r in await self._read("Settings"):
            if r.get("key"):
                v = r.get("value", "")
                default = DEFAULT_SETTINGS.get(r["key"])
                if isinstance(default, bool):
                    v = _truthy(v)
                elif isinstance(default, int) and str(v).strip() != "":
                    try:
                        v = int(float(v))
                    except ValueError:
                        v = default
                out[r["key"]] = v
        self._cache["settings"] = (time.monotonic(), out)
        return out

    def invalidate_cache(self) -> None:
        self._cache.clear()

    # ---------- customers ----------
    async def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        return next((r for r in await self._read("Customers") if r["customer_id"] == customer_id), None)

    async def find_customer(self, channel: str | None = None, sender_id: str | None = None,
                            phone: str | None = None) -> dict[str, Any] | None:
        rows = await self._read("Customers")
        if channel and sender_id:
            col = {"messenger": "messenger_psid", "instagram": "instagram_igsid"}[channel]
            hit = next((r for r in rows if str(r.get(col)) == str(sender_id)), None)
            if hit:
                return hit
        if phone:
            p = normalize_phone(phone)
            return next((r for r in rows if p and normalize_phone(r.get("phone")) == p), None)
        return None

    async def create_customer(self, fields: dict[str, Any]) -> str:
        now = clock.now()
        row = {"lead_status": "New", "do_not_contact": False, "total_jobs": 0, "lifetime_value": 0,
               "first_contact_at": now, "created_at": now, "updated_at": now, **fields}
        if row.get("phone"):
            row["phone"] = normalize_phone(row["phone"])
        return await self._create("Customers", row)

    async def update_customer(self, customer_id: str, fields: dict[str, Any]) -> None:
        if "phone" in fields:
            fields["phone"] = normalize_phone(fields["phone"])
        await self._update("Customers", customer_id, {**fields, "updated_at": clock.now()})

    async def append_customer_note(self, customer_id: str, note: str) -> None:
        c = await self.get_customer(customer_id) or {}
        stamp = clock.now().strftime("%m/%d")
        notes = (str(c.get("notes") or "") + f"\n[{stamp}] {note}").strip()
        await self.update_customer(customer_id, {"notes": notes})

    async def add_tags(self, customer_id: str, tags: list[str]) -> None:
        c = await self.get_customer(customer_id) or {}
        existing = [t.strip() for t in str(c.get("tags") or "").split(",") if t.strip()]
        merged = existing + [t for t in tags if t not in existing]
        await self.update_customer(customer_id, {"tags": ", ".join(merged)})

    async def list_customers(self) -> list[dict[str, Any]]:
        return await self._read("Customers")

    def is_dnc(self, customer: dict[str, Any] | None) -> bool:
        return bool(customer) and (_truthy(customer.get("do_not_contact")) or customer.get("lead_status") == "DNC")

    # ---------- vehicles ----------
    async def upsert_vehicle(self, customer_id: str, vehicle: dict[str, Any]) -> str:
        rows = [r for r in await self._read("Vehicles") if r["customer_id"] == customer_id]
        for r in rows:
            if (str(r.get("make", "")).lower(), str(r.get("model", "")).lower(), str(r.get("year", ""))) == (
                    str(vehicle.get("make", "")).lower(), str(vehicle.get("model", "")).lower(),
                    str(vehicle.get("year", ""))):
                await self._update("Vehicles", r["vehicle_id"], {k: v for k, v in vehicle.items() if v})
                return r["vehicle_id"]
        return await self._create("Vehicles", {**vehicle, "customer_id": customer_id, "created_at": clock.now()})

    async def list_vehicles(self, customer_id: str) -> list[dict[str, Any]]:
        return [r for r in await self._read("Vehicles") if r["customer_id"] == customer_id]

    # ---------- jobs ----------
    async def create_job(self, fields: dict[str, Any]) -> str:
        now = clock.now()
        return await self._create("Jobs", {"status": "Booked", "created_at": now, "updated_at": now, **fields})

    async def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        await self._update("Jobs", job_id, {**fields, "updated_at": clock.now()})

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return next((r for r in await self._read("Jobs") if r["job_id"] == job_id), None)

    async def list_jobs(self, customer_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self._read("Jobs")
        return [r for r in rows if customer_id is None or r["customer_id"] == customer_id]

    # ---------- follow-ups ----------
    async def create_follow_up(self, fields: dict[str, Any]) -> str:
        return await self._create("Follow-ups", {"status": "Open", "created_by": "agent", **fields})

    async def update_follow_up(self, follow_up_id: str, fields: dict[str, Any]) -> None:
        await self._update("Follow-ups", follow_up_id, fields)

    async def list_follow_ups(self, status: str | None = None, sender: str | None = None,
                              customer_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self._read("Follow-ups")
        return [r for r in rows if (status is None or r["status"] == status)
                and (sender is None or r["sender"] == sender)
                and (customer_id is None or r["customer_id"] == customer_id)]

    # ---------- approvals ----------
    async def create_approval(self, fields: dict[str, Any]) -> str:
        return await self._create("Approvals", {"status": "pending", "created_at": clock.now(), **fields})

    async def update_approval(self, approval_id: str, fields: dict[str, Any]) -> None:
        await self._update("Approvals", approval_id, fields)

    # ---------- messages ----------
    async def log_message(self, fields: dict[str, Any]) -> None:
        await self._run(self.store.append, "Messages", {k: _cell(v) for k, v in {"ts": clock.now(), **fields}.items()})
