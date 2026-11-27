"""Owner-run promotions (Settings tab: promo_keyword, promo_dm, promo_details, promo_ends)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app import clock

AMOUNT = re.compile(r"\$\s?(\d{1,5})(?:\.00)?\b")


def active_promo(settings: dict[str, Any], now: datetime | None = None) -> dict[str, str] | None:
    """The current promo, or None if it isn't set up or has ended (promo_ends is the last day, inclusive)."""
    kw = str(settings.get("promo_keyword") or "").strip()
    details = str(settings.get("promo_details") or "").strip()
    if not kw and not details:
        return None
    ends = str(settings.get("promo_ends") or "").strip()
    if ends:
        try:
            last = date.fromisoformat(ends[:10])
        except ValueError:
            try:
                m, d, *y = (int(x) for x in re.split(r"[/-]", ends))
                last = date(y[0] if y and y[0] > 99 else 2000 + (y[0] if y else clock.now().year % 100), m, d)
            except (ValueError, IndexError):
                return None  # unreadable end date: never run an offer open-ended
        if clock.to_local(now or clock.now()).date() > last:
            return None
    return {"keyword": kw, "dm": str(settings.get("promo_dm") or "").strip(), "details": details, "ends": ends}


def amounts(promo: dict[str, str] | None) -> set[int]:
    if not promo:
        return set()
    return {int(m) for m in AMOUNT.findall(promo["details"] + " " + promo["dm"])}


def keyword_hit(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", text or "", re.IGNORECASE) is not None
