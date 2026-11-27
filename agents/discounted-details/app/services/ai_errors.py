"""Plain-language alerts for Anthropic account problems (shared by Dex, Atlas, Nova and Pixel)."""

from __future__ import annotations

from app import clock

CREDITS_MSG = ("💳 Your Anthropic account is out of credits, so your agents can't think right now (Dex replies, Atlas, "
               "Nova captions, Pixel designs). Add credits at console.anthropic.com → Settings → Billing, and turn on "
               "auto-reload so it doesn't happen again. Buttons, scheduled posts and the offer DMs keep working.")


def is_credit_error(e: Exception) -> bool:
    return "credit balance" in str(e).lower()


async def alert_credits(dex) -> None:
    """At most one alert every 3 hours, however many messages fail."""
    last = dex.db.kv_get("credits_alert_at")
    if last and (clock.now() - clock.parse_iso(last)).total_seconds() < 3 * 3600:
        return
    dex.db.kv_set("credits_alert_at", clock.now().isoformat())
    await dex.owner.notify(CREDITS_MSG, urgent=True)
