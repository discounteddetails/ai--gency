# Dex: Discounted Details customer agent

Dex answers Discounted Details' Instagram and Messenger DMs (through Composio), quotes prices, books open times on Google Calendar, and keeps the Google Sheet CRM up to date. It asks the owner for approval on a private Telegram bot.

## Layout

| Path | What |
|---|---|
| `docs/PRD.md` | Full spec |
| `docs/AGENT_INSTRUCTIONS.md` | System prompt, loaded verbatim below the `---` line |
| `app/core.py` | The `Dex` runtime: inbound handling, safe sending, approvals, booking |
| `app/agent/` | Claude loop (`runner.py`), tools, handlers, per-turn context |
| `app/services/` | Sheets CRM, pricing, maps, calendar, scheduling, Composio, DM sender, Telegram, channels |
| `app/jobs/` | DM poller, scheduled jobs, APScheduler wiring |
| `app/nova/` | Nova, the social media agent: photo intake, privacy blur, captions, Story/post publishing |
| `app/atlas/` | Atlas, the operations manager: delegates to Dex/Nova/Pixel, Tasks tab, approval-gated business changes, morning brief (`docs/ATLAS_SPEC.md`) |
| `app/pixel/` | Pixel, the graphic designer: `/design` requests, HTML designs rendered in locked-down Chromium |
| `app/webhooks/telegram.py` | Owner bot: buttons, commands, typed replies |
| `scripts/` | Sheet bootstrap, Telegram setup, Human Agent test, OnePageCRM import |
| `tests/` | Unit and end-to-end tests with fakes (`tests/fakes.py`) |
| `evals/run.py` | Live-model evals (real API, costs money) |

## Commands

| Command | What it does |
|---|---|
| `make test` | Run the tests (`uv run pytest -q`) |
| `make lint` | Run ruff |
| `make run` | Run locally (needs `.env`) |
| `make eval` | Run the live evals (needs `ANTHROPIC_API_KEY`) |

## Non-negotiables

1. Hard guardrails are enforced in code (`app/guardrails.py`, `Dex.send_reply`), never only in the prompt.
2. Never send a DM outside the 24-hour window. The 1–7 day `HUMAN_AGENT` tag is only for owner-approved replies (`DMClient` asserts this).
3. The Google Sheet is the CRM of record. SQLite only holds runtime state.
4. All times are America/Chicago (`app/clock.py`).
5. Never hardcode prices outside `app/seed.py`. Live prices come from the Services tab.
6. Run with exactly **one** worker. The scheduler, debouncer and booking lock are in-process.
7. LLM history is append-only. Start a new epoch instead of trimming old turns.
8. Composio calls are allow-listed (`ALLOWED_TOOLS`). Dex must never comment, post or edit profiles. Only Nova publishes, and only after the owner taps ✅. Publish tools are never retried automatically (`NO_RETRY`).
9. Nova (`app/nova/`, spec in `docs/NOVA_SPEC.md`) never replies to comments, never puts "perfect" or addresses in captions, allows prices only in offer posts (`captions.check`), and blurs plates and house numbers before the owner sees a preview.
10. Atlas never writes prices, promos or settings directly: only `propose_change` → owner approves → `Dex.apply_business_changes`. Atlas delegates; it never duplicates another agent's job.
