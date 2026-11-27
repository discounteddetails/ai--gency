"""Register Dex's Telegram webhook and command menu (PRD §9.4).

Usage: uv run python scripts/telegram_setup.py
Needs TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, PUBLIC_BASE_URL. Prints the chat ID of anyone who sent /start
(run it once BEFORE the webhook is set if you don't know your chat ID yet: pass --chat-id-only).
"""

from __future__ import annotations

import os
import sys

import httpx

COMMANDS = [("today", "Today's jobs"), ("tomorrow", "Tomorrow's jobs"), ("status", "What's pending"),
            ("pause", "Stop customer replies"), ("resume", "Restart customer replies"),
            ("shadow_on", "Approve every reply first"), ("shadow_off", "Let Dex reply on its own"),
            ("help", "How to use Dex")]


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    base = f"https://api.telegram.org/bot{token}/"
    with httpx.Client(timeout=20) as h:
        if "--chat-id-only" in sys.argv:
            h.post(base + "deleteWebhook")
            ups = h.get(base + "getUpdates").json().get("result", [])
            ids = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in ups if "message" in u}
            print("Chats that messaged the bot:", ids or "none yet; send /start to your bot and re-run")
            return
        url = os.environ["PUBLIC_BASE_URL"].rstrip("/") + "/webhooks/telegram"
        r = h.post(base + "setWebhook", json={"url": url, "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"],
                                             "allowed_updates": ["message", "callback_query"],
                                             "drop_pending_updates": True})
        print("setWebhook:", r.json())
        r = h.post(base + "setMyCommands", json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        print("setMyCommands:", r.json())
        print("getWebhookInfo:", h.get(base + "getWebhookInfo").json().get("result"))


if __name__ == "__main__":
    main()
