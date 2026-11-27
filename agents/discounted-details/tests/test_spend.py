"""Spending tracker: per-request costs by agent, /spend, nightly Sheet rows, daily alert."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import time_machine

from app import clock
from app.services.spend import Ledger, TrackedClient, cost, parse_fixed, spend_text
from tests.fakes import World

NOW = datetime(2026, 12, 1, 9, 0, tzinfo=clock.TZ)  # Tue Dec 1


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=False) as t:
        yield t


def usage(i=0, o=0, cr=0, cw=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def test_cost_math():
    usd, _ = cost("claude-opus-5-5", usage(1_000_000, 100_000, 2_000_000, 0))
    assert usd == pytest.approx(4 + 2 + 0.4)
    assert cost("claude-sonnet-5-5", usage(1_000_000))[0] == pytest.approx(2)
    assert cost("claude-haiku-5-5", usage(1_000_000, 1_000_000))[0] == pytest.approx(0.6)
    assert cost("claude-mystery-9", usage(1_000_000))[0] == pytest.approx(4)  # unknown -> priciest
    assert parse_fixed("Railway 10; Composio: $29\nGoogle 0") == [("Railway", 10), ("Composio", 29), ("Google", 0)]


async def test_tracked_client_books_each_agent(frozen):
    w = World()
    ledger = Ledger(w.db)

    class Inner:
        async def create(self, **kw):
            return SimpleNamespace(model=kw["model"], usage=usage(100_000, 10_000))

    raw = SimpleNamespace(messages=Inner(), beta=SimpleNamespace(messages=Inner()))
    await TrackedClient(raw, ledger, "Dex").beta.messages.create(model="claude-opus-5-5")
    await TrackedClient(raw, ledger, "Pixel").messages.create(model="claude-sonnet-5-5")
    await TrackedClient(raw, ledger, "Pixel").messages.create(model="claude-sonnet-5-5")
    by = ledger.by_agent(NOW.date(), NOW.date())
    assert by["Dex"] == pytest.approx(0.6) and by["Pixel"] == pytest.approx(0.6)
    assert {r["agent"]: r["requests"] for r in ledger.rows(NOW.date())} == {"Dex": 1, "Pixel": 2}


async def test_spend_command_and_bills(frozen):
    w = World(settings_overrides={"monthly_fixed_costs": "Railway 10; Composio 29"})
    w.dex.ledger = ledger = Ledger(w.db)
    ledger.record("Dex", "claude-opus-5-5", usage(500_000))
    ledger.record("Atlas", "claude-sonnet-5-5", usage(500_000))
    ledger.count("composio_calls", 120)
    text = await w.bot.handle_text("/spend")
    assert "Today: $3.00 (Dex $2.00 · Atlas $1.00)" in text
    assert "Monthly bills: $39.00" in text and "This month total: $42.00" in text and "Composio calls" in text
    plain = await spend_text(ledger, {})
    assert "not set" in plain


async def test_nightly_rows_and_daily_alert(frozen):
    w = World(settings_overrides={"monthly_fixed_costs": "Railway 10", "daily_ai_alert_usd": 1})
    w.dex.ledger = ledger = Ledger(w.db)
    with time_machine.travel(NOW - timedelta(hours=12), tick=False):  # Nov 30 evening
        ledger.record("Nova", "claude-opus-5-5", usage(250_000))
    n = await w.jobs.book_spending()  # Dec 1 = the 1st: yesterday's AI + this month's bills
    rows = w.rows("Spending")
    assert n == 2 and rows[0]["agent"] == "Nova" and rows[0]["amount_usd"] == 1.0
    assert rows[1]["category"] == "Monthly bill" and rows[1]["item"] == "Railway"
    ledger.record("Pixel", "claude-sonnet-5-5", usage(1_000_000))
    await w.jobs.spend_alert()
    await w.jobs.spend_alert()
    alerts = [t for t in w.owner.texts("notify") if "AI costs today" in t]
    assert len(alerts) == 1 and "$2.00" in alerts[0]
