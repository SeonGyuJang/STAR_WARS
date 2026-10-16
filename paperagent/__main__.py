"""Command line:

    python -m paperagent serve [--host 0.0.0.0] [--port 8000]   dashboard + scheduler
    python -m paperagent run [--if-due]                        one pipeline cycle now
                                                               (--if-due: only if the interval has passed;
                                                                for an hourly Heroku Scheduler job)
    python -m paperagent initdb                                create tables (Heroku release phase)
    python -m paperagent review <arxiv id or url>              review one paper now
    python -m paperagent cards <paper id>                      re-render card news
    python -m paperagent trending                              refresh trending panels
    python -m paperagent demo [--clear]                        add (or remove) fictional sample data
"""
import argparse
import logging
import re
import sys

from . import db


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paperagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-scheduler", action="store_true")
    r_run = sub.add_parser("run")
    r_run.add_argument("--if-due", action="store_true", help="skip unless the configured interval has passed")
    sub.add_parser("initdb")
    r = sub.add_parser("review")
    r.add_argument("arxiv")
    c = sub.add_parser("cards")
    c.add_argument("paper_id", type=int)
    sub.add_parser("trending")
    d = sub.add_parser("demo")
    d.add_argument("--clear", action="store_true", help="remove the demo data instead")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()

    if args.cmd == "initdb":
        print("database ready")
    elif args.cmd == "serve":
        from . import create_app

        app = create_app(start_scheduler=not args.no_scheduler)
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    elif args.cmd == "run":
        from . import pipeline

        if args.if_due and not pipeline.is_due():
            print("not due yet; skipping")
            return
        db.mark_stale_runs()
        run_id = pipeline.run("schedule" if args.if_due else "cli")
        if run_id is None:
            print("another run is in progress; skipping")
            return
        print(next(r for r in db.recent_runs(5) if r["id"] == run_id)["log"])
    elif args.cmd == "review":
        from . import pipeline

        m = re.search(r"\d{4}\.\d{4,5}", args.arxiv)
        if not m:
            sys.exit("not an arXiv id")
        pid = pipeline.add_arxiv_paper(m.group(0))
        pipeline.run("cli", paper_ids=[pid])
        p = db.get_paper(pid)
        print(f"#{pid} {p['status']} overall={p['score_overall']} cards={len(p['card_paths'])}")
    elif args.cmd == "cards":
        from . import pipeline

        for path in pipeline.rerender_cards(args.paper_id):
            print(path)
    elif args.cmd == "trending":
        from .collectors import trending

        trending.refresh_all([k["term"] for k in db.list_keywords()], logger=print)
    elif args.cmd == "demo":
        from . import demo

        if args.clear:
            print(f"removed {demo.clear()} demo papers")
        else:
            pid = demo.seed()
            tid = demo.seed_tech()
            print(f"demo paper #{pid} and TECH post #{tid} added - open /paper/{pid} and /tech/{tid} on the dashboard")


if __name__ == "__main__":
    main()
