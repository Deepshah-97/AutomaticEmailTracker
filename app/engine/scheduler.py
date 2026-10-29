"""In-process APScheduler jobs. Run on exactly one instance (SCHEDULER_ENABLED=true)."""

import logging

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

from ..core import webhooks
from .check import FollowupEngine

log = logging.getLogger("agent.scheduler")


def build_scheduler(engine: FollowupEngine) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    http = httpx.Client(timeout=10)

    def deliver() -> None:
        webhooks.deliver_due(engine.session, http, engine.settings.webhook_max_attempts)

    sched.add_job(engine.run_due_checks, "interval", hours=1, id="checks")
    sched.add_job(engine.send_due, "interval", minutes=1, id="sends")
    sched.add_job(deliver, "interval", seconds=15, id="webhooks")
    sched.add_job(engine.recover_stale_sends, "interval", minutes=10, id="recover")
    sched.add_job(engine.check_alerts, "interval", hours=1, id="alerts")
    sched.add_job(engine.daily_summary, "cron", hour=22, minute=0, id="daily_summary")
    sched.add_job(engine.purge_old, "cron", hour=3, minute=0, id="retention")
    return sched
