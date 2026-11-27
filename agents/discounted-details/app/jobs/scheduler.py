"""APScheduler wiring (PRD §5.5). Runs in-process; the app must run with exactly one worker."""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import clock
from app.jobs.dm_poller import DMPoller
from app.jobs.tasks import Jobs

log = logging.getLogger(__name__)


NIGHT_START_HOUR, NIGHT_END_HOUR = 22, 6  # 10 PM - 6 AM Central: slower checks to save Composio calls


def is_night(hour: int) -> bool:
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


async def poll_if_due(poller, last_poll: dict[str, float], day_seconds: int, night_seconds: int) -> bool:
    """Called every `day_seconds`; at night only actually polls every `night_seconds`."""
    import time

    interval = night_seconds if is_night(clock.now().hour) else day_seconds
    now = time.monotonic()
    if now - last_poll.get("at", float("-inf")) < interval - 5:  # 5s slack for scheduler jitter
        return False
    last_poll["at"] = now
    await poller.poll_once()
    return True


def build_scheduler(jobs: Jobs, poller: DMPoller, poll_seconds: int, poll_seconds_night: int,
                    nova=None) -> AsyncIOScheduler:
    s = AsyncIOScheduler(timezone=clock.TZ, job_defaults={"coalesce": True, "max_instances": 1,
                                                          "misfire_grace_time": 300})

    last_poll: dict[str, float] = {}

    async def poll():
        await poll_if_due(poller, last_poll, poll_seconds, poll_seconds_night)

    def safe(fn):
        async def run():
            try:
                await fn()
            except Exception:
                log.exception("job %s failed", fn.__name__)
                try:
                    await jobs.dex.owner.notify(f"⚠️ Error in scheduled job {fn.__name__}")
                except Exception:
                    pass
        run.__name__ = fn.__name__
        return run

    s.add_job(safe(poll), IntervalTrigger(seconds=poll_seconds), id="dm_poll")
    s.add_job(safe(jobs.daily_summary), CronTrigger(hour=6, minute=30), id="daily_summary")
    s.add_job(safe(jobs.send_due_agent_followups), CronTrigger(hour="7-19", minute="*/15"), id="followups")
    s.add_job(safe(jobs.quote_checkins), CronTrigger(minute=5), id="quote_checkins")
    s.add_job(safe(jobs.owner_followup_digest), CronTrigger(hour=10, minute=0), id="owner_followup_digest")
    s.add_job(safe(jobs.auto_complete_jobs), CronTrigger(hour=21, minute=0), id="auto_complete")
    s.add_job(safe(jobs.post_job_followups), CronTrigger(hour=21, minute=5), id="post_job_followups")
    s.add_job(safe(jobs.approval_reminders), CronTrigger(minute="*/15"), id="approval_reminders")
    s.add_job(safe(jobs.flush_alerts), CronTrigger(minute="*/15"), id="flush_alerts")
    s.add_job(safe(jobs.reconcile_jobs), CronTrigger(hour=2, minute=0), id="reconcile_jobs")
    s.add_job(safe(jobs.retention_cleanup), CronTrigger(day_of_week="sun", hour=3), id="retention")
    s.add_job(safe(jobs.health_ping), IntervalTrigger(minutes=5), id="health_ping")
    if nova:
        async def nova_publish_due():
            await nova.publish_due()

        async def nova_media_cleanup():
            await asyncio.to_thread(nova.cleanup_media)

        s.add_job(safe(nova_publish_due), IntervalTrigger(minutes=5), id="nova_publish")
        s.add_job(safe(nova_media_cleanup), CronTrigger(hour=3, minute=30), id="nova_cleanup")
    return s
