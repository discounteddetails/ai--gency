# Manual follow-up workflow

One list a day, grouped by platform. Copy each message, send it, confirm. The agency handles the rest.

## Who does what
| | |
|---|---|
| **Dex** (owner) | Finds due follow-ups, checks the latest conversation, filters, dedupes, drafts, tracks attempts, keeps the CRM current (`app/services/followups.py`, `FollowUpDesk`) |
| **Atlas** (delivers) | Sends the one daily list at `followup_report_time` (Settings, default 10:00) |
| Retired | The separate 10 AM follow-up digest and the "Send today" line in the morning brief |

## The daily list (Telegram)
1. **Summary:** how many to send, by platform, and how to confirm. If nothing is due: "You're all caught up."
2. **One section per platform:** 📸 Instagram, 💬 Facebook Messenger, 📱 Text, 🟢 WhatsApp, 📝 Other.
3. **One numbered card per customer**, sorted by priority. Appointment checks come first, then sales follow-ups, then check-ins, reviews and rebooking. Each card has:
   - name or handle, and 🔥 for high priority
   - type, with the attempt number for sales (e.g. "Sales follow-up 2/3")
   - one sentence of context
   - the exact message, tap-to-copy
   - buttons: **📋 Copy message**, **💬 Open chat** (Instagram `ig.me` link, WhatsApp link with the text pre-filled, Messenger inbox), **✅ Sent** and **⏭ Skip**
   - a warning if the conversation couldn't be checked, or if the message looks like it was already sent
4. **"✅ I sent all N"**, which asks for confirmation before marking anything.

Commands: `/followups` (the list now), `/done 1 3`, `/done all`, `/skip 2`. Typing `done 1 3` also works.

## Rules
- **Due:** only follow-ups due today or overdue. Overdue ones say so and move up in priority.
- **Dedupe:** at most one open action per customer per kind (sales / appointment / review / rebook / check-in). Extras are cancelled with a note. Asking Dex twice updates the existing follow-up.
- **3-attempt limit:** at most 3 sales follow-ups. Each next one is created only when the previous one is confirmed sent (gaps of +3 days, then +2). After the 3rd, the lead is marked **Inactive**. Exception: the customer tag `keep_following_up`.
- **Dex handles it himself:** follow-ups Dex can send inside the 24-hour window aren't on the list (in test mode, they come as approval cards instead).
- **Conversation check:** for Instagram and Messenger, the latest messages are read first.
  - Customer wrote last: the sales follow-up is cancelled and the lead re-assessed.
  - Can't check: the card says so.
  - Text, WhatsApp and Other: the card says to check the chat first.
- **Never assumed:** a follow-up is only done when the owner confirms it. Repeated confirmations do nothing.
- **Kinds stay separate:** appointment checks, review requests, rebooking and check-ins never count as sales attempts.
- **Skipped automatically:** Do Not Contact, already booked (or an upcoming job), Lost and Inactive leads get no sales follow-ups.
- **Customer replies:** open sales follow-ups are cancelled and an Inactive lead becomes Quoted again. Review, rebook and appointment actions stay.

## When the owner confirms
- The Follow-ups row gets status **Sent**, `sent_at`, `completed_by` and the attempt number.
- The message goes into the Messages log.
- For sales, the next attempt is scheduled (or the lead is set Inactive after the 3rd).
- The item comes off today's list. Unconfirmed items stay pending with their attempt count unchanged.

## Sheet columns added to Follow-ups
`attempt, category, priority, context, completed_by` (appended after `notes`; existing columns unchanged).
