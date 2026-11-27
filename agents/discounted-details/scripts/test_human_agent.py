"""One-time live test: can Composio send a HUMAN_AGENT-tagged message (7-day window)? (PRD FR-H5)

Usage: uv run python scripts/test_human_agent.py --channel messenger --recipient <PSID> --text "Test from Dex"
Use a conversation with YOUR OWN test account whose last message is 1-7 days old. Prints PASS/FAIL so you know
whether to set human_agent_messenger / human_agent_instagram to TRUE in the Settings tab.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB  # noqa: E402
from app.services.composio_client import ComposioClient, ComposioError  # noqa: E402
from app.services.dm_client import DMClient  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", choices=["messenger", "instagram"], required=True)
    ap.add_argument("--recipient", required=True)
    ap.add_argument("--text", default="Test message from Discounted Details (please ignore).")
    a = ap.parse_args()
    c = ComposioClient(os.environ["COMPOSIO_API_KEY"], os.environ.get("COMPOSIO_USER_ID"),
                       os.environ.get("COMPOSIO_IG_ACCOUNT_ID"), os.environ.get("COMPOSIO_FB_ACCOUNT_ID"))
    dm = DMClient(c, DB(":memory:"), os.environ["FB_PAGE_ID"])
    try:
        mid = await dm.send_text(a.channel, a.recipient, a.text, human_agent=True, _approved_path=True)
        print(f"PASS: {a.channel} HUMAN_AGENT send worked (message {mid}). Set human_agent_{a.channel}=TRUE.")
    except ComposioError as e:
        print(f"FAIL: {a.channel} HUMAN_AGENT not available: {e}. Leave human_agent_{a.channel}=FALSE.")


if __name__ == "__main__":
    asyncio.run(main())
