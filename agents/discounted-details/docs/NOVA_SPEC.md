# Nova: Social Media Agent for Discounted Details

| | |
|---|---|
| **Role** | Turns the owner's job photos and videos into Instagram + Facebook Stories and slideshow posts |
| **Status** | V1, test stage: the owner approves every Story and post |
| **Runs in** | The same Railway service as Dex, and uses the same Telegram bot and Composio connections |
| **Date** | 2026-11-27 |

## 1. Goal

Grow reach on Instagram and Facebook so more people DM the business, which Dex then turns into bookings. Discovery findings (2026-11-27):
- Reach dropped from about 800 accounts per post in October to about 125 in November.
- 7 of the last 10 posts reused the same caption.
- The best post was a Reel that reached about 1,400 accounts.

## 2. Workflow

1. **The owner sends photos and videos to the Telegram bot** during or after a job. A note is optional, for example "Tahoe, Kyle".
2. **Batching:** Nova waits 3 minutes after the last upload, then processes everything as one batch.
3. **Nova looks at each item** with Claude vision and classifies it:
   - `on_the_way`: driving or arriving, with the truck, supplies or road visible.
   - `mid_job`: the owner working, with tools in use, foam or extraction.
   - `before`: a dirty vehicle.
   - `after`: a clean vehicle.
   - `skip`: blurry, duplicate or irrelevant.

   It also finds the screen regions of license plates and house or address numbers (to blur), and any faces.
4. **Privacy processing (photos):** license plates and house or address numbers are blurred automatically and subtly. In before/after posts, every face is blurred. Videos can't be blurred: if a sampled frame shows a plate, house number or face, the preview shows a ⚠️ warning for the owner to decide.
5. **Stories** (on-the-way and mid-job items) go to Telegram as previews with **✅ Post Story / ❌ Skip**. When the owner approves, Nova posts immediately:
   - **Photos:** Instagram Story and Facebook Page Story.
   - **Videos:** Instagram Story only. Composio supports only photo Stories on Facebook.
6. **Before/after slideshow post:**
   - Nova picks the best before and after photos (up to 6 in total, befores first) and writes the caption.
   - The preview goes to Telegram with **✅ Post / ✏️ Edit caption / ❌ Skip**.
   - When approved, the post is scheduled for the next evening slot (**6:30 PM, at most one post per day**). Extra approved posts move to the following days.
   - At the scheduled time it publishes as an **Instagram carousel** and a **Facebook multi-photo post**.
7. **Offer posts:** when the owner's note mentions a deal (or a photo is a flyer or promo graphic), every usable photo goes into one post, graphics first and uncropped. The caption leads with the running offer from the Settings tab and asks people to comment the promo keyword.
8. **Destinations:** every post card has toggles for IG feed / FB feed / IG Story / FB Story (default: both feeds), plus 🚀 Post now or the next 6:30 PM slot. The note can pick them, e.g. "facebook feed + instagram story". Story frames carry no caption; up to 5 photos each.
9. Every Story and post is logged in the Sheet's **Content** tab.

## 3. Caption rules (owner-approved)

- Always different, and written about that specific car and job, never a template.
- Professional and friendly, with 1–3 emojis.
- Ends with a reason to DM, for example "DM 'DETAIL' for a price on yours".
- **Exactly 5 hashtags** that fit the city and the work. For example: #kyletx #buda #austindetailing #mobiledetailing #cardetailing.
- **No prices**, except in posts about an offer (the owner's note mentions a deal, special or price): then the offer's prices and end date are stated exactly as the owner gave them. Captions the owner types may include prices.
- **City only.** Never a street, address or neighborhood.
- "New" is fine. **Never "perfect."** Also never "flawless," "guarantee" or "100%."
- Never a customer's name or anything identifying.

## 4. Hard rules (enforced in code)

| Rule | Where it's enforced |
|---|---|
| Nothing posts without the owner tapping ✅ | Approval state machine in `app/nova/service.py` |
| Plates and house numbers blurred in every photo before preview | `app/nova/media.py` (the preview shows the blurred version that will be posted) |
| Faces blurred in before/after posts; videos with risky frames are flagged | `media.py`, `service.py` |
| Caption lint: blocked words, `$` prices outside offer posts, street-address patterns, more than 5 hashtags | `app/nova/captions.py` |
| Never comments or replies | The Composio allowlist only includes publish/create tools for Nova |
| Media served publicly only through unguessable one-off URLs, deleted after 7 days | `/media/{token}` route, cleanup job |

## 5. Data

- **SQLite** (`nova_content`): one row per Story or post, holding processed file names, caption, warnings, approval state, schedule and published IDs.
- **Sheet `Content` tab:** `content_id, created_at, kind (story|post), status (pending|approved|scheduled|published|skipped|failed), scheduled_for, caption, platforms, ig_media_id, fb_post_id, notes`.

## 6. Limits and assumptions

| # | Item |
|---|---|
| N1 | Telegram bots can only download files up to **20 MB**. Videos should be short (about 30 seconds or less), or Nova asks for a shorter clip. |
| N2 | Facebook video Stories aren't available through Composio, so videos go to the Instagram Story only. |
| N3 | Blurring uses Claude's estimate of where plates and numbers are. The owner sees the exact blurred image before anything posts. |
| N4 | Instagram and Facebook must be able to fetch media from Dex's public Railway address (`/media/...`). |
| N5 | Reels are out of scope for now; the owner makes them. Slideshow posts only. |
| N6 | Publishing needs the Composio Instagram and Facebook connections to have content-publishing permission. This is verified on the first real post. |
| N7 | If Nova can match a batch to one of the day's jobs (Jobs tab, same day), it uses that job's vehicle, service and city for the caption. |
