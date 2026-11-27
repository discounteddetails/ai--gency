"""Owner Telegram webhook (PRD §5.3, §5.4, §9.4)."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import clock
from app.core import Dex

log = logging.getLogger(__name__)
_TASKS: set = set()
HELP = ("Commands:\n/today · /tomorrow — jobs\n/status — what's pending\n/pause · /resume — stop/start replies\n"
        "/shadow_on · /shadow_off — approve every reply first\nOr type anything, e.g. \"who's booked Saturday\" or "
        "\"promo 15% off interiors for dog owners\".\nApprovals: tap the buttons, or type yes A17 / no A17 / edit A17 <msg>.")
TYPED = re.compile(r"^\s*(yes|y|ok|👍|no|n|edit)\b\s*(?:a?(\d+))?\s*(.*)$", re.IGNORECASE | re.DOTALL)


class OwnerBot:
    def __init__(self, dex: Dex, owner_chat_id: int, secret: str):
        self.dex = dex
        self.chat_id = int(owner_chat_id)
        self.secret = secret
        self.nova = None  # set by main when Nova is enabled

    # ---------------- pending typed input (Edit / Set price / Answer) ----------------
    def _set_pending(self, approval_id: str, action: str) -> None:
        self.dex.db.kv_set("telegram_pending_input", {"approval_id": approval_id, "action": action,
                                                      "at": clock.now().isoformat()})

    def _pop_pending(self) -> dict[str, Any] | None:
        p = self.dex.db.kv_get("telegram_pending_input")
        self.dex.db.kv_set("telegram_pending_input", None)
        if p and clock.now() - clock.parse_iso(p["at"]) < timedelta(hours=2):
            return p
        return None

    async def handle_update(self, update: dict[str, Any]) -> None:
        if cq := update.get("callback_query"):
            if int(cq.get("message", {}).get("chat", {}).get("id", 0)) != self.chat_id:
                return
            await self._callback(cq)
            return
        msg = update.get("message") or {}
        chat = int(msg.get("chat", {}).get("id", 0))
        if chat != self.chat_id:
            if chat and hasattr(self.dex.owner, "send_to"):
                await self.dex.owner.send_to(chat, "This is a private bot.")
            log.warning("ignored Telegram update from chat %s", chat)
            return
        if self.nova and (msg.get("photo") or msg.get("video") or msg.get("document")):
            if await self.nova.add_media(msg):
                return
        text = (msg.get("text") or "").strip()
        if text and self.nova and self.nova.collecting() and not text.startswith("/") \
                and not self.dex.db.kv_get("telegram_pending_input"):
            self.nova.add_note(text)
            await self.dex.owner.notify("📝 Added that note to the photos.", urgent=True)
            return
        if text:
            reply = await self.handle_text(text)
            if reply:
                await self.dex.owner.notify(reply, urgent=True)

    async def _callback(self, cq: dict[str, Any]) -> None:
        data = str(cq.get("data", ""))
        await self.dex.owner.answer_callback(cq["id"])
        try:
            aid, action = data.split(":", 1)
        except ValueError:
            return
        if aid.startswith("N") and self.nova:
            reply = await self.nova.handle_callback(aid, action, cq.get("message", {}).get("message_id"))
            if reply and action in ("post", "now", "skip") and cq.get("message", {}).get("message_id"):
                await self.dex.owner.mark_decided(cq["message"]["message_id"], reply) \
                    if cq["message"].get("text") else None
            if reply:
                await self.dex.owner.notify(reply, urgent=True)
            return
        if aid.startswith("F") and action in ("sent", "skip"):
            await self.dex.crm.update_follow_up(aid, {"status": "Sent" if action == "sent" else "Skipped",
                                                      **({"sent_at": clock.now()} if action == "sent" else {})})
            if cq.get("message", {}).get("message_id"):
                await self.dex.owner.mark_decided(cq["message"]["message_id"],
                                                  f"{aid} · {'✅ Sent' if action == 'sent' else '⏭ Skipped'}")
            return
        appr = self.dex.db.get_approval(aid)
        if not appr or appr["status"] != "pending":
            await self.dex.owner.notify(f"{aid} was already decided.", urgent=True)
            return
        if action in ("edit", "price", "answer"):
            self._set_pending(aid, action)
            prompt = {"edit": f"✏️ {aid}: send the exact message for the customer.",
                      "price": f"💲 {aid}: send the price (just the number, e.g. 250).",
                      "answer": f"💬 {aid}: type your answer. Dex will word it for the customer."}[action]
            await self.dex.owner.ask_for_text(prompt)
            return
        result = await self.dex.decide(aid, action)
        await self.dex.owner.notify(result, urgent=True)

    async def handle_text(self, text: str) -> str | None:
        dex = self.dex
        if pending := self._pop_pending():
            aid, action = pending["approval_id"], pending["action"]
            if action == "nova_caption" and self.nova:
                return await self.nova.set_caption(aid, text) or None
            if action == "price":
                m = re.search(r"\d+", text.replace(",", ""))
                if not m:
                    self._set_pending(aid, action)
                    return "Send just the number, e.g. 250."
                return await dex.decide(aid, "price", price=int(m.group(0)))
            return await dex.decide(aid, action, text=text)
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/pause":
            dex.db.kv_set("global_pause", True)
            return "⏸ Dex is paused. New DMs will be forwarded to you here. /resume to restart."
        if cmd == "/resume":
            dex.db.kv_set("global_pause", False)
            return "▶️ Dex is back on."
        if cmd == "/shadow_on":
            dex.db.kv_set("shadow_mode", True)
            return "Shadow mode ON: every reply comes to you first."
        if cmd == "/shadow_off":
            dex.db.kv_set("shadow_mode", False)
            return "Shadow mode OFF: Dex replies on its own (approvals still come to you)."
        if cmd == "/status":
            return await status_text(dex)
        if cmd in ("/today", "/tomorrow"):
            day = clock.now().date() + timedelta(days=1 if cmd == "/tomorrow" else 0)
            return await jobs_text(dex, day)
        if m := TYPED.match(text):
            word, num, rest = m.group(1).lower(), m.group(2), m.group(3).strip()
            pending_ids = [a["approval_id"] for a in dex.db.list_approvals("pending")]
            if num:
                aid = next((p for p in pending_ids if int(re.sub(r"\D", "", p)) == int(num)), None)
                if not aid:
                    return f"No pending approval A{num}."
            elif len(pending_ids) == 1:
                aid = pending_ids[0]
            elif not pending_ids:
                aid = None
            else:
                return "Which one? " + " or ".join(pending_ids[:5])
            if aid:
                if word == "edit":
                    return await dex.decide(aid, "edit", text=rest) if rest else "Add the message after edit."
                if word in ("yes", "y", "ok", "👍"):
                    price = re.search(r"\$?\s*(\d+)", rest)
                    if price and dex.db.get_approval(aid)["type"] in ("discount", "custom_quote"):
                        return await dex.decide(aid, "price", price=int(price.group(1)))
                    return await dex.decide(aid, "approve")
                return await dex.decide(aid, "decline")
        if dex.runner:
            replies = await dex.runner.run_owner_command(text)
            return None if replies else "Done."
        return None


async def jobs_text(dex: Dex, day) -> str:
    jobs = []
    for j in await dex.crm.list_jobs():
        try:
            start = clock.parse_iso(str(j["start"]))
        except ValueError:
            continue
        if start.date() == day and j.get("status") in ("Booked", "Completed"):
            c = await dex.crm.get_customer(j["customer_id"])
            city = str(j.get("address", "")).split(",")[1].strip() if "," in str(j.get("address", "")) else ""
            jobs.append((start, f" {start.strftime('%-I:%M%p')[:-1]} {j['service'].title()} - {dex.display_name(c)}"
                                f"{', ' + city if city else ''} ${j['total']}"))
    head = f"{day.strftime('%a %-m/%-d')}: {len(jobs)} job{'s' if len(jobs) != 1 else ''}"
    return "\n".join([head] + [t for _, t in sorted(jobs)])


async def status_text(dex: Dex) -> str:
    pending = dex.db.list_approvals("pending")
    fus = [f for f in await dex.crm.list_follow_ups(status="Open", sender="owner")
           if str(f.get("due_at", "")) <= clock.now().strftime("%Y-%m-%d %H:%M")]
    paused = [t for t in dex.db.list_threads() if dex.is_paused(t)]
    return (f"Agent: {'PAUSED' if dex.paused else 'ON'} | Shadow: {'ON' if dex.shadow else 'OFF'} | "
            f"Pending approvals: {len(pending)} | Owner follow-ups due: {len(fus)} | Paused threads: {len(paused)}")


def router(bot: OwnerBot) -> APIRouter:
    r = APIRouter()

    @r.post("/webhooks/telegram")
    async def telegram_webhook(request: Request):
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(token, bot.secret):
            raise HTTPException(403)
        update = await request.json()

        async def work():
            try:
                await bot.handle_update(update)
            except Exception:
                log.exception("telegram update failed")

        task = asyncio.create_task(work())  # answer Telegram immediately; agent runs can take a while
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return {"ok": True}

    return r
