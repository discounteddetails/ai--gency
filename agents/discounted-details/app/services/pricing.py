"""Pricing engine (PRD §6.2). Prices come only from the Services tab rows passed in."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

SERVICES = ("exterior", "interior", "full")
SIZE_CLASSES = ("sedan", "small_suv_truck", "large_suv", "van")
ADDONS = {"pet_hair": "addon_pet_hair", "stains": "addon_stains"}
CONDITIONS = ("standard", "worse", "bad", "terrible")
INTERIOR_SERVICES = ("interior", "full")


class PricingError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class Quote:
    service: str
    service_name: str
    size_class: str
    base: int
    addons: list[dict[str, Any]] = field(default_factory=list)
    condition: str = "standard"
    condition_surcharge: int = 0
    travel_miles: float | None = None
    travel_fee: int = 0
    total: int = 0
    duration_min: int = 0
    in_service_area: bool | None = None
    requires_approval: bool = False
    approval_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def amounts(self) -> set[int]:
        """Every dollar figure a customer could legitimately be told for this quote (guardrail price integrity)."""
        vals = {self.base, self.total, self.travel_fee, self.condition_surcharge}
        vals |= {a["price"] for a in self.addons}
        vals.add(self.total - self.travel_fee)
        return {v for v in vals if v}


def _active(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if str(r.get("active", True)).strip().upper() not in ("FALSE", "0", "NO")]


def _row(rows: list[dict[str, Any]], key: str, size_class: str = "any") -> dict[str, Any]:
    for r in _active(rows):
        if r["service_key"] == key and r["size_class"] in (size_class, "any"):
            return r
    raise PricingError("SERVICE_NOT_FOUND", f"{key}/{size_class}")


def _int(v: Any) -> int:
    return int(round(float(v)))


def quote(service: str, size_class: str, addons: list[str], condition: str, miles: float | None,
          services_rows: list[dict[str, Any]], settings: dict[str, Any]) -> Quote:
    if service not in SERVICES:
        raise PricingError("INVALID_SERVICE", service)
    if size_class not in SIZE_CLASSES:
        raise PricingError("INVALID_SIZE_CLASS", size_class)
    condition = condition or "standard"
    if condition not in CONDITIONS:
        raise PricingError("INVALID_CONDITION", condition)
    addons = list(dict.fromkeys(addons or []))
    if addons and service not in INTERIOR_SERVICES:
        raise PricingError("ADDON_NOT_APPLICABLE", "Pet hair and stain removal are Interior/Full only.")
    if condition != "standard" and service not in INTERIOR_SERVICES:
        raise PricingError("CONDITION_NOT_APPLICABLE", "Condition surcharge applies to Interior/Full only.")

    base_row = _row(services_rows, service, size_class)
    q = Quote(service=service, service_name=base_row["name"], size_class=size_class,
              base=_int(base_row["price"]), duration_min=_int(base_row["duration_min"]), condition=condition)
    for a in addons:
        if a not in ADDONS:
            raise PricingError("INVALID_ADDON", a)
        r = _row(services_rows, ADDONS[a])
        q.addons.append({"key": a, "name": r["name"], "price": _int(r["price"])})
        q.duration_min += _int(r["duration_min"])
    if condition != "standard":
        r = _row(services_rows, f"condition_{condition}")
        q.condition_surcharge = _int(r["price"])
        q.duration_min += _int(r["duration_min"])

    free_miles = float(settings.get("travel_free_miles", 25))
    per_mile = float(settings.get("travel_fee_per_mile", 2))
    approval_over = float(settings.get("custom_quote_over_miles", 60))
    if miles is not None:
        q.travel_miles = round(miles, 1)
        extra = max(0.0, miles - free_miles)
        q.travel_fee = int(per_mile * math.ceil(round(extra, 6)))
        q.in_service_area = miles <= free_miles
        if miles > approval_over:
            q.requires_approval = True
            q.approval_reason = f"{miles:.0f} miles away (over {approval_over:.0f})"
    q.total = q.base + sum(a["price"] for a in q.addons) + q.condition_surcharge + q.travel_fee
    return q
