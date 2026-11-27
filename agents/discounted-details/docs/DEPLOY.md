# Deploying Dex: step-by-step setup

This guide is written for the owner, not a developer. Each step says where to click and what to copy. Keep every key and token private. They go into Railway, never into a chat or a file in the repo.

**Already done:**
- Composio is connected to Instagram and the Facebook Page.
- The Telegram bot is created and your chat ID is known.
- The "Discounted Details CRM" Google Sheet is set up.
- Railway is connected to Claude.

---

## 1. Google Cloud (about 20 minutes)

Dex needs a Google "service account": a robot account that can read and write your Sheet and Calendar and use Maps.

1. Go to **console.cloud.google.com** and sign in with your business Gmail.
2. Create a project at the top: **Select a project → New project**, name it "Dex", then **Create**.
3. **Turn on the APIs.** Go to **APIs & Services → Library**, search for each of these and click **Enable**:
   - Google Sheets API
   - Google Drive API
   - Google Calendar API
   - Geocoding API
   - Routes API
4. **Create the service account.**
   1. Go to **APIs & Services → Credentials → Create credentials → Service account**.
   2. Name it "dex" and click **Done**.
   3. Click the new service account, go to **Keys → Add key → Create new key → JSON**. A `.json` file downloads. This is `GOOGLE_SERVICE_ACCOUNT_JSON`.
   4. Copy the service account's email address. It looks like `dex@dex-123456.iam.gserviceaccount.com`.
5. **Create the Maps key.** Go to **Credentials → Create credentials → API key**, then click **Restrict key** and allow only the Geocoding API and Routes API. This is `GOOGLE_MAPS_API_KEY`.
6. **Billing:** Maps requires a billing account, but your volume stays within Google's free monthly usage. Set a **budget alert at $10** under **Billing → Budgets & alerts**.
7. **Share your Sheet** with the service-account email as **Editor**: open the Sheet, click **Share**, and paste the email.
8. **Share your Calendar** with the service-account email:
   1. Open **calendar.google.com**.
   2. Go to **⚙ Settings**, then click your calendar under "Settings for my calendars".
   3. Under **Share with specific people**, click **Add people**, paste the email and choose **"Make changes to events"**.

## 2. Composio API key (about 2 minutes)

In the Composio dashboard, go to **Settings → API Keys → Create**. This is `COMPOSIO_API_KEY`.

Check your plan covers about **80,000 tool calls a month**, which is what checking every 60 seconds uses. If it doesn't, set `POLL_SECONDS=90`.

## 3. Anthropic API key (about 5 minutes)

At **platform.claude.com**:
1. Add billing.
2. Under **Limits**, set a monthly spend limit of **$50**.
3. Under **API Keys → Create Key**, create a key. This is `ANTHROPIC_API_KEY`.

## 4. Railway (about 15 minutes)

1. Go to **railway.com → New Project → Deploy from GitHub repo** and pick `ai--gency`.
2. In the service's **Settings**:
   - Set **Root Directory** to `agents/discounted-details`.
   - Set **Branch** to the branch with Dex's code.
   - Railway finds the `Dockerfile` automatically.
3. Under **Settings → Volumes → New Volume**, set the mount path to **`/data`** (1 GB is plenty).
4. Under **Settings → Networking → Generate Domain**, copy the URL, for example `https://dex-production.up.railway.app`. This is `PUBLIC_BASE_URL`.
5. Under **Variables**, add everything from `.env.example`:

   | Variable | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | from step 3 |
   | `COMPOSIO_API_KEY` | from step 2 |
   | `COMPOSIO_IG_ACCOUNT_ID` / `COMPOSIO_FB_ACCOUNT_ID` | already filled in `.env.example` |
   | `FB_PAGE_ID` / `IG_USER_ID` | already filled in `.env.example` |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | open the downloaded `.json` file and paste its **entire** contents |
   | `GOOGLE_SHEET_ID` | already filled in `.env.example` |
   | `GOOGLE_CALENDAR_ID` | your business Gmail address |
   | `GOOGLE_MAPS_API_KEY` | from step 1 |
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `TELEGRAM_OWNER_CHAT_ID` | your chat ID |
   | `TELEGRAM_WEBHOOK_SECRET` | any random 32+ characters (Railway can generate one) |
   | `SLOT_SIGNING_KEY` | any random 64 characters |
   | `PUBLIC_BASE_URL` | from step 4 |
   | `SHADOW_MODE_DEFAULT` | `true` |

6. Deploy. Then open `PUBLIC_BASE_URL/healthz`. It should show `{"ok": true}`.

## 5. Connect the Telegram bot (about 2 minutes)

In Railway, open the service's **shell**, or run the following with the Railway CLI:

```
python scripts/telegram_setup.py
```

Then send `/status` to your bot in Telegram. It should reply with `Agent: ON | Shadow: ON | …`.

## 6. First week: shadow mode

Shadow mode is on. Every reply Dex wants to send arrives in Telegram with **Send / Edit / Skip** buttons.

1. Test it yourself:
   - DM your Instagram from a friend's account: "how much for a full detail on a 2019 Civic?"
   - You should get a draft in Telegram within about 90 seconds.
   - Tap **Send**.
2. Book a test appointment end-to-end. Check that the calendar event and the Sheet rows look right.
3. Optional: test the 7-day replies on each channel and turn on the ones that pass (Settings tab: `human_agent_messenger` / `human_agent_instagram`):

   ```
   python scripts/test_human_agent.py --channel messenger --recipient <a friend's ID> --text "test"
   ```

4. After about 20 good drafts, send `/shadow_off`. Dex then replies on its own, and approvals still come to you.

## Everyday controls (Telegram)

| Command | What it does |
|---|---|
| `/today`, `/tomorrow` | Your jobs |
| `/status` | What's pending |
| `/pause`, `/resume` | Stop or start Dex replying (paused DMs are forwarded to you) |
| `/shadow_on`, `/shadow_off` | Approve every reply first, or let Dex send on its own |
| Plain text | Anything else, for example "who's booked Saturday?" or "promo 15% off interiors for dog owners" |

## If something breaks

| What you see | What to do |
|---|---|
| "Dex can't read Instagram/Messenger" | Reconnect Instagram or Facebook in the Composio dashboard |
| "Dex is down (AI unavailable)" | Anthropic is having an outage. Reply to customers yourself; Dex catches up automatically. |
| "Daily AI budget reached" | Something unusual happened. Check Railway logs, then send `/resume`. |
| Anything else | In Railway, open the service, then **Deployments → View logs**, or ask Claude to look (Railway is connected) |
