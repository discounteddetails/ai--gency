# Atlas: Operations Manager for Discounted Details

| | |
|---|---|
| **Role** | The owner's main contact. Turns objectives into work for Dex, Nova and Pixel, tracks it, checks it and reports back |
| **Pattern** | Orchestrator–worker. Atlas is a Claude agent whose tools *are* the other agents. No duplicated skills |
| **Runs in** | The same always-on Railway service (Claude API tool use), same Telegram bot and Sheet |
| **Instructions** | `docs/ATLAS_INSTRUCTIONS.md` (system prompt below the `---` line) |
| **Date** | 2026-11-27 |

## Why not Claude Code at runtime
Claude Code builds and maintains these agents. It isn't a 24/7 service: it can't receive Telegram webhooks or run on a schedule without a person. Atlas uses the same orchestrator–worker idea as Claude Code subagents, but inside the existing service. That way it reuses every integration (Composio, Google, Telegram) and the existing approval system.

## Routing (Telegram)
| Message | Goes to |
|---|---|
| Plain text | **Atlas**, with team status (Pixel's current design, Nova's pending post, pending approvals) |
| Starts with "Dex," / "Nova," / "Pixel," | Straight to that agent (router) |
| `/design`, `/offer_story`, `/style`, buttons, pending-input replies, `yes A12` | Unchanged |
| `/tasks`, `/brief` | Atlas |

## Tools (all delegate; Atlas never re-implements)
| Tool | Uses |
|---|---|
| `delegate_to_dex` | Dex's existing owner-command mode, run quietly. Dex's reply comes back to Atlas instead of going to Telegram. Dex keeps all of its own approvals |
| `start_design`, `change_design`, `pixel_style_rule` | Pixel's job system. Options still go to the owner to pick |
| `nova_status`, `nova_offer_story` | Nova's content store and offer Story |
| `business_snapshot` | Live Sheet data: services, settings, promo, jobs, leads, approvals, follow-ups, content, tasks, activity |
| `update_task` | The **Tasks** tab (T####) |
| `propose_change` | An owner approval card (type `business_change`). The change is written to the Sheet only on ✅ Approve |

## Approval rules (enforced in code)
- Atlas has **no tool that writes prices, promos or settings directly**. `propose_change` only creates an approval, and `Dex.apply_business_changes` runs only from the approval's Approve path.
- Only the `Settings` and `Services` tabs can be changed that way. Anything else is rejected.
- Spending: there is no tool that spends money. Atlas recommends and asks.
- Dex's existing approvals are unchanged: discounts, custom quotes, cancellations, complaints, bulk messages and late replies.

## Operations
- **6:30 AM brief** replaces Dex's morning summary (no duplicate messages). It includes:
  - Dex's summary,
  - Nova's posts today and items waiting,
  - Atlas's open tasks,
  - health checks,
  - and on **Mondays**, 2 improvement ideas.
- **Health checks** (rule-based, no AI cost):
  - offer ended but promo settings still filled in,
  - duplicate or unpriced Services rows,
  - approvals waiting over 12h,
  - Nova posts that failed or have waited over 24h,
  - Pixel jobs stuck over 20 minutes (stopped automatically),
  - tasks with no update in 3+ days,
  - high AI usage.
- **Activity feed:** Atlas's actions show in the HQ office feed.
- **Budget:** Atlas counts toward the same daily AI budget as Dex.
