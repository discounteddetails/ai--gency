"""Claude tool loop (PRD §7.4). Manual loop over the Messages API: one in-flight run per thread, append-only history,
all tool results returned in a single user message, refusal / max_tokens escalate to the owner."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import anthropic

from app import clock
from app.agent.context import build_turn
from app.agent.handlers import ToolContext, run_tool
from app.agent.prompt import system_prompt
from app.agent.tools import TOOLS
from app.core import Dex
from app.guardrails import TurnState

log = logging.getLogger(__name__)
MAX_ITERATIONS = 10
MAX_TURNS_PER_EPOCH = 30


def _block_to_param(block: Any) -> dict[str, Any]:
    """Keep every block exactly as returned (thinking blocks included) so history is replayed unchanged."""
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_none=True)


class AgentRunner:
    def __init__(self, dex: Dex, client: anthropic.AsyncAnthropic, model: str, daily_token_budget: int):
        self.dex = dex
        self.client = client
        self.model = model
        self.budget = daily_token_budget
        dex.runner = self

    # ------------------------------------------------------------------ public entry points
    async def run_customer_turn(self, thread_id: str, events: list[Any]) -> None:
        async with self.dex.thread_lock(thread_id):
            thread = self.dex.db.get_thread(thread_id)
            if not thread or self.dex.is_paused(thread):
                return
            customer = await self.dex.crm.get_customer(thread["customer_id"]) if thread.get("customer_id") else None
            body = "\n".join(e.text or "[customer sent an image or attachment I can't open]" for e in events)
            await self._run("CUSTOMER_CONVERSATION", thread, customer, "customer_message", body, history_key=thread_id)

    async def run_approval_result(self, thread_id: str, approval_id: str, type_: str, decision: str,
                                  payload: dict[str, Any], text: str | None) -> None:
        async with self.dex.thread_lock(thread_id):
            thread = self.dex.db.get_thread(thread_id)
            customer = await self.dex.crm.get_customer(payload.get("customer_id") or "")
            body_lines = [f"approval_id: {approval_id}", f"type: {type_}", f"decision: {decision}"]
            if payload.get("override_price") is not None:
                body_lines.append(f"approved_price: ${payload['override_price']}")
            if payload.get("new_start"):
                body_lines.append(f"new_time: {clock.label(clock.parse_iso(payload['new_start']))}")
            if type_ == "question" and text:
                body_lines.append(f"owner_answer: {text}")
            state = TurnState(approved_confirmation=decision == "approved" and type_ in ("reschedule", "cancel"))
            if payload.get("override_price") is not None:
                state.verified_amounts.add(int(payload["override_price"]))
            await self._run("APPROVAL_RESULT", thread, customer, "approval_result", "\n".join(body_lines),
                            history_key=thread_id, state=state)

    async def run_owner_command(self, text: str) -> list[str]:
        ctx = await self._run("OWNER_COMMAND", None, None, "owner_command", text, history_key="owner")
        return ctx.owner_replies if ctx else []

    async def run_scheduled_task(self, customer: dict[str, Any], task: str) -> None:
        thread = self.dex.thread_for(customer)
        await self._run("SCHEDULED_TASK", thread, customer, "scheduled_task", task,
                        history_key=f"scheduled:{customer['customer_id']}:{uuid.uuid4().hex[:6]}", kind="proactive")

    # ------------------------------------------------------------------ loop
    def _history_key(self, base: str) -> str:
        epoch = int(self.dex.db.kv_get(f"epoch:{base}", 0))
        key = f"{base}#e{epoch}"
        if self._turn_count(key) >= MAX_TURNS_PER_EPOCH:
            # Start a fresh conversation instead of trimming old turns (history is append-only);
            # the CRM context block carries the customer's memory forward.
            self.dex.db.kv_set(f"epoch:{base}", epoch + 1)
            key = f"{base}#e{epoch + 1}"
        return key

    def _turn_count(self, key: str) -> int:
        rows = self.dex.db._q("SELECT COUNT(DISTINCT turn_id) n FROM llm_history WHERE thread_id=?", (key,))
        return int(rows[0]["n"]) if rows else 0

    def _budget_ok(self) -> bool:
        used = int(self.dex.db.kv_get(f"tokens_{clock.now():%Y%m%d}", 0))
        return used < self.budget

    def _track(self, usage: Any) -> None:
        if not usage:
            return
        n = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0) * 5 + \
            (getattr(usage, "cache_creation_input_tokens", 0) or 0) + \
            int((getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1)
        key = f"tokens_{clock.now():%Y%m%d}"
        self.dex.db.kv_set(key, int(self.dex.db.kv_get(key, 0)) + n)

    async def _context(self, mode: str, thread, customer, body_tag: str, body: str) -> str:
        crm = self.dex.crm
        cid = (customer or {}).get("customer_id")
        approvals = [a for a in self.dex.db.list_approvals("pending") if a["payload"].get("customer_id") == cid] if cid else []
        return build_turn(
            mode=mode, now=clock.now(), thread=thread, customer=customer,
            vehicles=await crm.list_vehicles(cid) if cid else [], jobs=await crm.list_jobs(cid) if cid else [],
            follow_ups=await crm.list_follow_ups(status="Open", customer_id=cid) if cid else [],
            approvals=approvals, services=await crm.get_services(), settings=await crm.get_settings(),
            dnc=crm.is_dnc(customer), body_tag=body_tag, body=body)

    async def _create(self, messages: list[dict[str, Any]]):
        return await self.client.beta.messages.create(
            model=self.model,
            max_tokens=4096,
            system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    async def _run(self, mode: str, thread, customer, body_tag: str, body: str, *, history_key: str,
                   state: TurnState | None = None, kind: str = "reply") -> ToolContext | None:
        dex = self.dex
        who = dex.display_name(customer) if customer else "owner"
        if not self._budget_ok():
            dex.db.kv_set("global_pause", True)
            await dex.owner.notify("⛔ Daily AI budget reached. Dex is paused; send /resume to restart.", urgent=True)
            return None
        state = state or TurnState()
        state.shadow = dex.shadow
        ctx = ToolContext(dex=dex, mode=mode, customer=customer, thread=thread, state=state,
                          turn_id=uuid.uuid4().hex[:12], kind=kind)
        key = self._history_key(history_key)
        messages = dex.db.load_history(key, MAX_TURNS_PER_EPOCH)
        user_msg = {"role": "user", "content": [{"type": "text",
                                                  "text": await self._context(mode, thread, customer, body_tag, body)}]}
        messages.append(user_msg)
        dex.db.append_history(key, ctx.turn_id, "user", user_msg["content"])
        started = time.monotonic()
        for _ in range(MAX_ITERATIONS):
            response = None
            for attempt in range(3):
                try:
                    response = await self._create(messages)
                    break
                except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
                    log.warning("Claude call failed (%s), attempt %s", type(e).__name__, attempt + 1)
                    await asyncio.sleep(10 * (attempt + 1))
                except anthropic.APIStatusError as e:
                    if e.status_code >= 500:
                        await asyncio.sleep(10 * (attempt + 1))
                        continue
                    log.error("Claude request rejected: %s", e)
                    await dex.owner.notify(f"⚠️ Dex error ({e.status_code}) on a message from {who}. "
                                           "Please reply to them yourself.", urgent=True)
                    return ctx
            if response is None:
                await dex.owner.notify(f"⚠️ Dex is down (AI unavailable). Message from {who}: \"{body[:300]}\"",
                                       urgent=True)
                return ctx
            self._track(getattr(response, "usage", None))
            content = [_block_to_param(b) for b in response.content]
            messages.append({"role": "assistant", "content": content})
            dex.db.append_history(key, ctx.turn_id, "assistant", content)

            if response.stop_reason in ("refusal", "max_tokens"):
                await dex.owner.notify(f"⚠️ Dex couldn't handle a message from {who} ({response.stop_reason}). "
                                       f"Their message: \"{body[:300]}\"", urgent=True)
                return ctx
            if response.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                log.info("turn done mode=%s thread=%s in %.1fs", mode, history_key, time.monotonic() - started)
                return ctx
            results = []
            for tu in tool_uses:  # sequential: keeps message order deterministic
                out, is_err = await run_tool(tu.name, dict(tu.input), ctx)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out,
                                 **({"is_error": True} if is_err else {})})
            tool_msg = {"role": "user", "content": results}
            messages.append(tool_msg)
            dex.db.append_history(key, ctx.turn_id, "user", results)
        await dex.owner.notify(f"⚠️ Dex hit its step limit on a conversation with {who}. Please check it.",
                               urgent=True)
        return ctx
