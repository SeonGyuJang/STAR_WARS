"""In-process scheduler for single-server installs.

Every 30 minutes it checks whether the dashboard's run interval has passed
since the last successful run, so restarts don't trigger extra (paid) runs.
On Heroku the same check runs from the Heroku Scheduler add-on instead
(`python -m paperagent run --if-due`), and this scheduler stays off."""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, pipeline

log = logging.getLogger(__name__)
JOB_ID = "paper-pipeline-tick"
_scheduler = None


def _tick():
    if pipeline.is_due() and not pipeline.is_running():
        pipeline.run("schedule")


def start():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _tick,
        "interval",
        minutes=30,
        id=JOB_ID,
        next_run_time=datetime.now().astimezone() + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("scheduler started: checks every 30 min, runs every %s h", db.pipeline_settings()["interval_hours"])
    return _scheduler


def reschedule(hours: float):
    """The interval is read from settings on every tick; nothing to reschedule."""


def next_run():
    """When the next run is due: last successful run + interval."""
    last = db.last_finished_run()
    if not last:
        return None
    started = datetime.fromisoformat(last["started_at"])
    return started + timedelta(hours=db.pipeline_settings()["interval_hours"])
