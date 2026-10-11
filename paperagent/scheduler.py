"""Background scheduler that repeats the pipeline every N hours."""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, pipeline

log = logging.getLogger(__name__)
JOB_ID = "paper-pipeline"
_scheduler = None


def start():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    hours = db.pipeline_settings()["interval_hours"]
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        pipeline.run,
        "interval",
        hours=hours,
        id=JOB_ID,
        kwargs={"trigger": "schedule"},
        # first run shortly after startup, then every `hours`
        next_run_time=datetime.now().astimezone() + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    log.info("scheduler started: every %s h", hours)
    return _scheduler


def reschedule(hours: float):
    if _scheduler is None:
        return
    _scheduler.reschedule_job(JOB_ID, trigger="interval", hours=hours)


def next_run():
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None
