"""Create any missing CRM tabs/headers and seed Services + Settings if empty (PRD Step 4).

Usage: uv run python scripts/bootstrap_sheet.py   (reads GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID)
Safe to re-run: never overwrites rows that already exist.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.seed import SERVICES_ROWS, SETTINGS_ROWS, TABS  # noqa: E402


def main() -> None:
    import gspread

    gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    ss = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    existing = {ws.title: ws for ws in ss.worksheets()}
    for tab, headers in TABS.items():
        ws = existing.get(tab) or ss.add_worksheet(tab, rows=1000, cols=len(headers))
        first = ws.row_values(1)
        if not first:
            ws.update([headers], "A1")
            ws.freeze(rows=1)
            print(f"created headers for {tab}")
        else:
            missing = [h for h in headers if h not in first]
            if missing:
                print(f"WARNING {tab} is missing columns: {missing}")
    services = existing.get("Services") or ss.worksheet("Services")
    if len(services.get_all_values()) <= 1:
        h = TABS["Services"]
        services.append_rows([[r.get(k, "") for k in h] for r in SERVICES_ROWS], value_input_option="USER_ENTERED")
        print("seeded Services")
    settings = existing.get("Settings") or ss.worksheet("Settings")
    if len(settings.get_all_values()) <= 1:
        settings.append_rows([[k, ("'" + v) if isinstance(v, str) and ":" in v else v, d]
                              for k, v, d in SETTINGS_ROWS], value_input_option="USER_ENTERED")
        print("seeded Settings")
    print("done")


if __name__ == "__main__":
    main()
