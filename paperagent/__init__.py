"""STAR WARS - Smart Topic Analysis & Rating : Worthwise Article Research System.

A Claude agent that reads new papers, rates them and publishes a dashboard."""
import logging
import os


def create_app(start_scheduler: bool = True):
    from flask import Flask

    from . import config, db, web

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    db.mark_stale_runs()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.register_blueprint(web.bp)

    # Under the Flask debug reloader only the child process should schedule.
    in_reloader_parent = app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if start_scheduler and config.SCHEDULER_ENABLED and not in_reloader_parent:
        from . import scheduler

        scheduler.start()
    return app
