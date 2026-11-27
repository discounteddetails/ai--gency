"""Spending tracker: the dollar cost of every Claude request, by agent, plus fixed monthly bills.
Estimates come from the token usage Anthropic returns on each response; the Anthropic Console bill is the final word."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app import clock

log = logging.getLogger(__name__)

# $ per 1M tokens: input, output, cache read, cache write (5-minute). Anthropic list prices, 2026-11.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
SCHEMA = """CREATE TABLE IF NOT EXISTS spend (day TEXT, agent TEXT, model TEXT, requests INTEGER, input INTEGER,
  output INTEGER, cache_read INTEGER, cache_write INTEGER, usd REAL, PRIMARY KEY (day, agent, model));
CREATE TABLE IF NOT EXISTS counters (day TEXT, name TEXT, n INTEGER, PRIMARY KEY (day, name));"""


def price_for(model: str) -> tuple[float, float, float, float]:
    model = (model or "").split("@")[0]
    for k in sorted(PRICES, key=len, reverse=True):
        if model.startswith(k):
            return PRICES[k]
    return PRICES["claude-opus-5-5"]  # unknown: assume the priciest we use, never under-report


def cost(model: str, usage: Any) -> tuple[float, dict[str, int]]:
    def g(k: str) -> int:
        return int((usage.get(k, 0) if isinstance(usage, dict) else getattr(usage, k, 0)) or 0)

    t = {"input": g("input_tokens"), "output": g("output_tokens"), "cache_read": g("cache_read_input_tokens"),
         "cache_write": g("cache_creation_input_tokens")}
    pi, po, pr, pw = price_for(model)
    usd = (t["input"] * pi + t["output"] * po + t["cache_read"] * pr + t["cache_write"] * pw) / 1_000_000
    return usd, t


class Ledger:
    def __init__(self, db):
        self.db = db
        with db._lock:
            db._conn.executescript(SCHEMA)

    def record(self, agent: str, model: str, usage: Any) -> float:
        if usage is None:
            return 0.0
        usd, t = cost(model, usage)
        day = clock.now().date().isoformat()
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO spend VALUES (?,?,?,1,?,?,?,?,?) ON CONFLICT(day, agent, model) DO UPDATE SET "
                "requests=requests+1, input=input+excluded.input, output=output+excluded.output, "
                "cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write, "
                "usd=usd+excluded.usd",
                (day, agent, model, t["input"], t["output"], t["cache_read"], t["cache_write"], usd))
        return usd

    def count(self, name: str, n: int = 1) -> None:
        day = clock.now().date().isoformat()
        with self.db._lock:
            self.db._conn.execute("INSERT INTO counters VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET n=n+?",
                                  (day, name, n, n))

    def by_agent(self, start: date, end: date) -> dict[str, float]:
        rows = self.db._q("SELECT agent, SUM(usd) usd FROM spend WHERE day>=? AND day<=? GROUP BY agent",
                          (start.isoformat(), end.isoformat()))
        return {r["agent"]: float(r["usd"] or 0) for r in rows}

    def rows(self, day: date) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db._q("SELECT * FROM spend WHERE day=?", (day.isoformat(),))]

    def counter(self, name: str, start: date, end: date) -> int:
        r = self.db._q("SELECT SUM(n) n FROM counters WHERE name=? AND day>=? AND day<=?",
                       (name, start.isoformat(), end.isoformat()))
        return int(r[0]["n"] or 0) if r else 0


class _Messages:
    def __init__(self, inner, ledger: Ledger, agent: str):
        self._inner, self._ledger, self._agent = inner, ledger, agent

    async def create(self, **kw):
        resp = await self._inner.create(**kw)
        try:
            self._ledger.record(self._agent, getattr(resp, "model", None) or kw.get("model", ""),
                                getattr(resp, "usage", None))
        except Exception:
            log.exception("spend tracking failed")
        return resp

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TrackedClient:
    """Wraps the Anthropic client so every request's cost is booked to one agent."""

    def __init__(self, client, ledger: Ledger, agent: str):
        self._client = client
        self.messages = _Messages(client.messages, ledger, agent)
        self.beta = SimpleNamespace(messages=_Messages(client.beta.messages, ledger, agent))

    def __getattr__(self, name):
        return getattr(self._client, name)


def parse_fixed(text: str) -> list[tuple[str, float]]:
    """'Railway 10; Composio 29' -> [('Railway', 10.0), ('Composio', 29.0)]"""
    import re
    out = []
    for part in re.split(r"[;\n|]", text or ""):
        m = re.match(r"\s*(.+?)\s*[:=]?\s*\$?\s*(\d+(?:\.\d+)?)\s*$", part)
        if m:
            out.append((m.group(1).strip(), float(m.group(2))))
    return out


def month_start(d: date) -> date:
    return d.replace(day=1)


async def spend_text(ledger: Ledger, settings: dict[str, Any], now: datetime | None = None) -> str:
    now = now or clock.now()
    today = now.date()
    week0 = today - timedelta(days=today.weekday())
    m0 = month_start(today)

    def line(d: dict[str, float]) -> str:
        total = sum(d.values())
        parts = " · ".join(f"{a} ${v:.2f}" for a, v in sorted(d.items(), key=lambda x: -x[1]) if v >= 0.005)
        return f"${total:.2f}" + (f" ({parts})" if parts else "")

    month_ai = ledger.by_agent(m0, today)
    fixed = parse_fixed(str(settings.get("monthly_fixed_costs") or ""))
    fixed_total = sum(v for _, v in fixed)
    out = ["💰 Spending",
           f"Today: {line(ledger.by_agent(today, today))}",
           f"This week: {line(ledger.by_agent(week0, today))}",
           f"This month (AI): {line(month_ai)}"]
    if fixed:
        out.append(f"Monthly bills: ${fixed_total:.2f} (" + " · ".join(f"{n} ${v:.0f}" for n, v in fixed) + ")")
        out.append(f"This month total: ${sum(month_ai.values()) + fixed_total:.2f}")
    else:
        out.append("Monthly bills: not set. Add them on the Settings tab (monthly_fixed_costs, e.g. Railway 10; "
                   "Composio 29).")
    calls = ledger.counter("composio_calls", m0, today)
    if calls:
        out.append(f"Composio calls this month: {calls:,}")
    out.append("AI costs are estimates from usage; your Anthropic bill is the final word.")
    return "\n".join(out)
