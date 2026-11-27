"""FastAPI app: health checks, Telegram webhook, and the in-process scheduler (DM poller + jobs)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse

from app.config import get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
               "event": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def build(settings=None):
    """Wire real services. Returns (dex, runner, jobs, poller, owner_bot)."""
    from app.agent.runner import AgentRunner
    from app.core import Dex, DexConfig
    from app.db import DB
    from app.jobs.dm_poller import DMPoller
    from app.jobs.tasks import Jobs
    from app.services.calendar import GoogleCalendar
    from app.services.composio_client import ComposioClient
    from app.services.dm_client import DMClient
    from app.services.maps import GoogleMaps
    from app.services.owner_telegram import TelegramOwner
    from app.services.sheets import GspreadStore, SheetsCRM
    from app.webhooks.telegram import OwnerBot

    s = settings or get_settings()
    sa = s.GOOGLE_SERVICE_ACCOUNT_JSON.get_secret_value()
    db = DB(s.DB_PATH)
    crm = SheetsCRM(GspreadStore(sa, s.GOOGLE_SHEET_ID))
    composio = ComposioClient(s.COMPOSIO_API_KEY.get_secret_value(), s.COMPOSIO_USER_ID, s.COMPOSIO_IG_ACCOUNT_ID,
                              s.COMPOSIO_FB_ACCOUNT_ID)
    owner = TelegramOwner(s.TELEGRAM_BOT_TOKEN.get_secret_value(), s.TELEGRAM_OWNER_CHAT_ID)
    dex = Dex(cfg=DexConfig(signing_key=s.SLOT_SIGNING_KEY.get_secret_value(),
                            owner_takeover_hours=s.OWNER_TAKEOVER_HOURS, debounce_seconds=s.DEBOUNCE_SECONDS,
                            shadow_default=s.SHADOW_MODE_DEFAULT,
                            sheet_url=f"https://docs.google.com/spreadsheets/d/{s.GOOGLE_SHEET_ID}"),
              db=db, crm=crm, calendar=GoogleCalendar(sa, s.GOOGLE_CALENDAR_ID),
              maps=GoogleMaps(s.GOOGLE_MAPS_API_KEY.get_secret_value()),
              dm=DMClient(composio, db, s.FB_PAGE_ID), owner=owner)
    runner = AgentRunner(dex, anthropic.AsyncAnthropic(api_key=s.ANTHROPIC_API_KEY.get_secret_value(),
                                                       max_retries=3, timeout=60),
                         s.ANTHROPIC_MODEL, s.DAILY_TOKEN_BUDGET)

    async def poll_alert(streak: int):
        await owner.notify("⚠️ Dex can't read Instagram/Messenger right now (5 failed checks). If this continues, "
                           "reconnect Instagram/Facebook in Composio.", urgent=True)

    poller = DMPoller(composio, db, page_id=s.FB_PAGE_ID, ig_user_id=s.IG_USER_ID,
                      on_customer_message=dex.on_customer_message, on_owner_message=dex.on_owner_message,
                      on_failure_streak=poll_alert)
    jobs = Jobs(dex, s.HEALTHCHECK_URL)
    bot = OwnerBot(dex, s.TELEGRAM_OWNER_CHAT_ID, s.TELEGRAM_WEBHOOK_SECRET.get_secret_value())
    if s.NOVA_ENABLED:
        from app.nova.media import MediaHost
        from app.nova.service import Nova
        from app.nova.vision import Vision

        bot.nova = Nova(dex, Vision(runner.client, s.ANTHROPIC_MODEL), MediaHost(s.NOVA_MEDIA_DIR, s.PUBLIC_BASE_URL),
                        bot_token=s.TELEGRAM_BOT_TOKEN.get_secret_value(), ig_user_id=s.IG_USER_ID,
                        page_id=s.FB_PAGE_ID, batch_seconds=s.NOVA_BATCH_SECONDS)
    if s.PIXEL_ENABLED:
        from app.nova.media import MediaHost
        from app.pixel.designer import Designer
        from app.pixel.render import Renderer
        from app.pixel.service import Pixel

        host = bot.nova.host if bot.nova else MediaHost(s.NOVA_MEDIA_DIR, s.PUBLIC_BASE_URL)
        bot.pixel = Pixel(dex, Designer(runner.client, s.ANTHROPIC_MODEL), Renderer(s.CHROMIUM_PATH or None), host,
                          s.PIXEL_ASSETS_DIR, bot_token=s.TELEGRAM_BOT_TOKEN.get_secret_value(), nova=bot.nova)
    from app.router import Router

    bot.router = Router(runner.client, s.ANTHROPIC_MODEL, pixel=bot.pixel, nova=bot.nova, dex=dex)
    return dex, runner, jobs, poller, bot


def create_app() -> FastAPI:
    from app.jobs.scheduler import build_scheduler
    from app.webhooks.telegram import router

    s = get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=s.LOG_LEVEL, handlers=[handler], force=True)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    # httpx logs full request URLs; the Telegram bot token is part of the URL, so never log them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    dex, runner, jobs, poller, bot = build(s)
    from app.jobs.comment_watcher import CommentWatcher

    comments = CommentWatcher(dex, ig_user_id=s.IG_USER_ID, page_id=s.FB_PAGE_ID)
    sched = build_scheduler(jobs, poller, s.POLL_SECONDS, s.POLL_SECONDS_NIGHT, nova=bot.nova, comments=comments)

    async def register_telegram() -> None:
        """Point the Telegram bot at this server on every start (idempotent)."""
        import httpx

        from scripts.telegram_setup import COMMANDS

        base = f"https://api.telegram.org/bot{s.TELEGRAM_BOT_TOKEN.get_secret_value()}/"
        url = s.PUBLIC_BASE_URL.rstrip("/") + "/webhooks/telegram"
        log = logging.getLogger("dex")
        try:
            async with httpx.AsyncClient(timeout=15) as h:
                r = await h.post(base + "setWebhook", json={
                    "url": url, "secret_token": s.TELEGRAM_WEBHOOK_SECRET.get_secret_value(),
                    "allowed_updates": ["message", "callback_query"]})
                log.info("telegram setWebhook ok=%s %s", r.json().get("ok"), r.json().get("description", ""))
                await h.post(base + "setMyCommands",
                             json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        except Exception as e:
            log.error("telegram registration failed: %s", e)

    async def announce() -> None:
        await register_telegram()
        try:
            await asyncio.wait_for(dex.owner.notify(
                f"👋 Dex is online. Test mode is {'ON: every reply comes to you here first' if dex.shadow else 'OFF'}. "
                "Send /help to see what I can do.", urgent=True), timeout=30)
        except Exception:
            logging.getLogger("dex").exception("startup notify failed")

    background: set = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched.start()
        logging.getLogger("dex").info("Dex started (shadow=%s)", dex.shadow)
        t = asyncio.create_task(announce())  # never block startup on network calls
        background.add(t)
        t.add_done_callback(background.discard)
        yield
        sched.shutdown(wait=False)

    app = FastAPI(title="Dex", lifespan=lifespan)
    app.include_router(router(bot))
    ready_cache: dict[str, float] = {}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/media/{name}")
    async def media(name: str):
        """Nova's processed photos/videos, fetched by Instagram, Facebook and Telegram. Names are unguessable."""
        host = bot.nova.host if bot.nova else (bot.pixel.host if bot.pixel else None)
        p = host.path(name) if host else None
        if not p:
            raise HTTPException(status_code=404)
        return FileResponse(p, media_type="video/mp4" if p.suffix == ".mp4" else "image/jpeg")

    @app.get("/readyz")
    async def readyz(response: Response):
        if time.monotonic() - ready_cache.get("ok_at", 0) < 60:
            return {"ok": True, "cached": True}
        try:
            dex.db.kv_get("ping")
            await dex.crm.get_settings()
        except Exception as e:
            response.status_code = 503
            return {"ok": False, "error": type(e).__name__}
        ready_cache["ok_at"] = time.monotonic()
        return {"ok": True}

    return app


def health_only_app() -> FastAPI:
    """Used by tests (no external services)."""
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
