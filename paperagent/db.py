"""Storage for papers, settings, runs and blobs.

SQLAlchemy Core keeps one code path for SQLite (local runs) and Postgres
(Heroku, where the filesystem is wiped on every dyno restart). Timestamps are
stored as ISO-8601 UTC strings and compared as strings, which keeps queries
free of dialect-specific date functions."""
import json
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (Column, Float, Integer, LargeBinary, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, delete, event, func, inspect, select, text, update)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.pool import NullPool

from . import config

metadata = MetaData()

keywords = Table(
    "keywords", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("term", String(200), nullable=False, unique=True),
    Column("created_at", String(32), nullable=False),
)

# kind: 'arxiv' (value = arXiv category, e.g. cs.CL) or
#       'journal' - a journal or conference. value = OpenAlex source id (e.g. S4306420609),
#       'venue:<key>' for a well-known venue (collectors.venues.KNOWN) not found in OpenAlex,
#       or 'name:<name>' until the pipeline looks it up.
# meta (JSON): {"type": "conference"|"journal", "full": ..., "openalex_id": ..., "arxiv_terms": [...]}
sources = Table(
    "sources", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(20), nullable=False),
    Column("value", String(300), nullable=False),
    Column("name", String(300), nullable=False),
    Column("enabled", Integer, nullable=False, default=1),
    Column("meta", Text),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("kind", "value"),
)

papers = Table(
    "papers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(300), nullable=False, unique=True),
    Column("source_kind", String(20), nullable=False),
    Column("source_name", String(300)),
    # the configured source that first found it, and the journal/conference it
    # appeared in (also set when a venue finds a paper we already had)
    Column("source_id", Integer, index=True),
    Column("venue", String(300)),
    Column("title", Text, nullable=False),
    Column("authors", Text, nullable=False, default="[]"),
    Column("abstract", Text),
    Column("published", String(40)),
    Column("url", Text),
    Column("pdf_url", Text),
    Column("doi", String(300)),
    Column("categories", Text, nullable=False, default="[]"),
    Column("matched_keywords", Text, nullable=False, default="[]"),
    Column("relevance", Float),
    Column("relevance_reason", Text),
    Column("status", String(20), nullable=False, default="new", index=True),
    Column("read_depth", String(20)),
    Column("review", Text),
    Column("score_method", Float),
    Column("score_novelty", Float),
    Column("score_overall", Float),
    Column("cost_usd", Float),
    # when the review finished (what "최신순" on the dashboard follows)
    Column("reviewed_at", String(32), index=True),
    # > 0 when an admin picked the paper by hand (read first, even if below the relevance threshold);
    # -1 when an admin set it aside (stays under 관련도 미달 whatever its relevance)
    Column("priority", Integer, default=0),
    Column("card_paths", Text, nullable=False, default="[]"),
    # slide number -> storage key of the MP4 version of that slide (video card)
    Column("card_videos", Text),
    Column("citation_count", Integer),
    Column("error", Text),
    Column("created_at", String(32), nullable=False, index=True),
    Column("updated_at", String(32), nullable=False),
)

# Trending projects and tech news (GitHub Trending, Hacker News, GeekNews,
# Hugging Face), made into TECH card news. uid: "github:owner/repo",
# "hn:<id>", "geeknews:<id>", "hf:model/<id>", "hf:space/<id>".
# status: new -> screened (worth a post) | skipped -> done | error
# meta (JSON): numbers from the source (stars, points, likes, ...), author, discussion link
# media (JSON): {"image": storage key, "video": storage key, "image_url": ..., "video_url": ...}
tech_items = Table(
    "tech_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(300), nullable=False, unique=True),
    Column("source", String(20), nullable=False, index=True),
    Column("title", Text, nullable=False),
    Column("url", Text),
    Column("summary", Text),
    Column("meta", Text),
    Column("media", Text),
    Column("interest", Float),
    Column("interest_reason", Text),
    Column("status", String(20), nullable=False, default="new", index=True),
    Column("review", Text),
    Column("card_paths", Text, nullable=False, default="[]"),
    Column("card_videos", Text),
    Column("cost_usd", Float),
    Column("error", Text),
    Column("reviewed_at", String(32), index=True),
    Column("created_at", String(32), nullable=False, index=True),
    Column("updated_at", String(32), nullable=False),
)

runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trigger", String(20), nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", String(32), nullable=False),
    Column("finished_at", String(32)),
    Column("stats", Text, nullable=False, default="{}"),
    Column("log", Text, nullable=False, default=""),
    # live progress for the dashboard console: phase, papers to read, current one, stage
    Column("progress", Text),
)

settings_t = Table(
    "settings", metadata,
    Column("key", String(100), primary_key=True),
    Column("value", Text, nullable=False),
)

citation_snapshots = Table(
    "citation_snapshots", metadata,
    Column("uid", String(300), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("count", Integer, nullable=False),
)

trending_cache = Table(
    "trending_cache", metadata,
    Column("kind", String(40), primary_key=True),
    Column("payload", Text, nullable=False),
    Column("updated_at", String(32), nullable=False),
)

# Card images and extracted figures when STORAGE_BACKEND=db (see storage.py).
blobs = Table(
    "blobs", metadata,
    Column("key", String(300), primary_key=True),
    Column("content_type", String(80), nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("updated_at", String(32), nullable=False),
)

JSON_FIELDS = ("authors", "categories", "matched_keywords", "review", "card_paths", "card_videos", "meta", "media")

DEFAULT_KEYWORDS = [
    "large language model",
    "AI agent",
    "reinforcement learning from human feedback",
    "multimodal",
    "diffusion model",
    "retrieval-augmented generation",
]
DEFAULT_ARXIV = [
    ("cs.AI", "arXiv · Artificial Intelligence"),
    ("cs.CL", "arXiv · Computation and Language"),
    ("cs.LG", "arXiv · Machine Learning"),
    ("cs.CV", "arXiv · Computer Vision"),
]
# Journals are resolved to OpenAlex source ids on first use (see collectors.openalex).
DEFAULT_JOURNALS = [
    "Journal of Machine Learning Research",
    "IEEE Transactions on Pattern Analysis and Machine Intelligence",
    "Nature Machine Intelligence",
    "Transactions on Machine Learning Research",
]

_engine = None
_engine_url = None


def engine():
    global _engine, _engine_url
    url = config.database_url()
    if _engine is None or url != _engine_url:
        if url.startswith("sqlite"):
            _engine = create_engine(url, poolclass=NullPool, connect_args={"check_same_thread": False, "timeout": 30})

            @event.listens_for(_engine, "connect")
            def _wal(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
        else:
            _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        _engine_url = url
    return _engine


def _insert(table):
    """Dialect insert that supports on_conflict_do_nothing / do_update."""
    dialect = postgresql if engine().dialect.name == "postgresql" else sqlite
    return dialect.insert(table)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ago_iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(microsecond=0).isoformat()


def _row(row):
    if row is None:
        return None
    d = dict(row._mapping)
    for f in JSON_FIELDS:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except json.JSONDecodeError:
                pass
    return d


def _rows(result):
    return [_row(r) for r in result]


def _add_missing_columns():
    """Lightweight migration: add columns that exist in the code but not yet in
    the database (e.g. after deploying a new version to Heroku). Columns are
    added as nullable; nothing is ever dropped or changed."""
    eng = engine()
    existing_tables = set(inspect(eng).get_table_names())
    with eng.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspect(conn).get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(dialect=eng.dialect)}'
                    conn.execute(text(ddl))


_LOG_LINE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] (.*)$", re.M)


def review_times_from_runs() -> dict:
    """{paper_id: UTC ISO time} of each paper's last successful review, read
    from the run logs ("reading #12: ..." followed by "  reviewed (...)").
    Log lines carry only a time of day - UTC in older logs, Korea time since -
    so each run's zone is the one that puts its first line closest to the run's
    start, and the date comes from the start."""
    out = {}
    with engine().connect() as conn:
        rows = conn.execute(select(runs.c.started_at, runs.c.log).order_by(runs.c.id)).all()
    for started, log in rows:
        lines = _LOG_LINE.findall(log or "")
        if not lines:
            continue
        start = datetime.fromisoformat(started)

        def at(local_start, h, m, sec):
            t = local_start.replace(hour=int(h), minute=int(m), second=int(sec), microsecond=0)
            return t + timedelta(days=1) if t < local_start - timedelta(minutes=5) else t

        zone = min((timezone.utc, config.TIMEZONE),
                   key=lambda z: abs(at(start.astimezone(z), *lines[0][:3]) - start.astimezone(z)))
        base, prev, current = start.astimezone(zone), None, None
        for h, m, sec, text in lines:
            t = at(base, h, m, sec)
            while prev and t < prev:  # past midnight
                t += timedelta(days=1)
            prev = t
            reading = re.match(r"reading #(\d+):", text)
            if reading:
                current = int(reading.group(1))
            elif current and text.lstrip().startswith("reviewed ("):
                out[current] = t.astimezone(timezone.utc).isoformat()
    return out


def backfill_review_times():
    """Give reviews made before reviewed_at existed their real review time: from
    the run logs, else when their cards were rendered. Runs once."""
    if get_setting("reviewed_at_v2"):
        return
    from . import storage  # storage imports db

    from_logs = review_times_from_runs()
    with engine().begin() as conn:
        done = conn.execute(select(papers.c.id, papers.c.card_paths).where(papers.c.status == "done")).all()
        for pid, cards in done:
            when = from_logs.get(pid)
            if not when:
                keys = json.loads(cards or "[]")
                when = storage.stored_at(keys[0]) if keys else None
            if when:  # a direct update: this is not a change to the paper
                conn.execute(update(papers).where(papers.c.id == pid).values(reviewed_at=when))
        conn.execute(update(papers).where(papers.c.status == "done", papers.c.reviewed_at.is_(None))
                     .values(reviewed_at=papers.c.updated_at))
    set_setting("reviewed_at_v2", True)


def init_db(seed: bool = True):
    metadata.create_all(engine())
    _add_missing_columns()
    backfill_review_times()
    if not seed or get_setting("seeded"):
        return
    ts = now_iso()
    with engine().begin() as conn:
        for term in DEFAULT_KEYWORDS:
            conn.execute(_insert(keywords).values(term=term, created_at=ts).on_conflict_do_nothing())
        for value, name in DEFAULT_ARXIV:
            conn.execute(_insert(sources).values(kind="arxiv", value=value, name=name, enabled=1, created_at=ts)
                         .on_conflict_do_nothing())
        # Unresolved journals keep value 'name:<journal name>' until the pipeline
        # looks up their OpenAlex id.
        for name in DEFAULT_JOURNALS:
            conn.execute(_insert(sources).values(kind="journal", value=f"name:{name}", name=name, enabled=1, created_at=ts)
                         .on_conflict_do_nothing())
    set_setting("seeded", True)


# --- settings ---------------------------------------------------------------

def get_setting(key, default=None):
    with engine().connect() as conn:
        value = conn.execute(select(settings_t.c.value).where(settings_t.c.key == key)).scalar()
    return default if value is None else json.loads(value)


def set_setting(key, value):
    stmt = _insert(settings_t).values(key=key, value=json.dumps(value))
    with engine().begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value}))


def pipeline_settings() -> dict:
    return {
        "interval_hours": get_setting("interval_hours", config.DEFAULT_INTERVAL_HOURS),
        "lookback_days": get_setting("lookback_days", config.DEFAULT_LOOKBACK_DAYS),
        "max_per_source": get_setting("max_per_source", config.DEFAULT_MAX_PER_SOURCE),
        "max_full_reads": get_setting("max_full_reads", config.DEFAULT_MAX_FULL_READS),
        "min_relevance": get_setting("min_relevance", config.DEFAULT_MIN_RELEVANCE),
    }


# --- keywords & sources -----------------------------------------------------

def list_keywords():
    with engine().connect() as conn:
        return _rows(conn.execute(select(keywords).order_by(func.lower(keywords.c.term))))


def add_keyword(term: str) -> bool:
    term = term.strip()
    if not term:
        return False
    with engine().begin() as conn:
        exists = conn.execute(select(keywords.c.id).where(func.lower(keywords.c.term) == term.lower())).first()
        if exists:
            return False
        conn.execute(keywords.insert().values(term=term, created_at=now_iso()))
        return True


def delete_keyword(keyword_id: int):
    with engine().begin() as conn:
        conn.execute(delete(keywords).where(keywords.c.id == keyword_id))


def list_sources(kind=None, enabled_only=False):
    q = select(sources)
    if kind:
        q = q.where(sources.c.kind == kind)
    if enabled_only:
        q = q.where(sources.c.enabled == 1)
    with engine().connect() as conn:
        return _rows(conn.execute(q.order_by(sources.c.kind, sources.c.name)))


def add_source(kind: str, value: str, name: str, meta=None):
    """Returns the new source id, or None if it was already there."""
    with engine().begin() as conn:
        result = conn.execute(_insert(sources).values(kind=kind, value=value, name=name, enabled=1, created_at=now_iso(),
                                                      meta=json.dumps(meta, ensure_ascii=False) if meta else None)
                              .on_conflict_do_nothing().returning(sources.c.id))
        row = result.first()
        return row[0] if row else None


def get_source(source_id: int):
    with engine().connect() as conn:
        return _row(conn.execute(select(sources).where(sources.c.id == source_id)).first())


def update_source(source_id: int, **fields):
    if isinstance(fields.get("meta"), dict):
        fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
    if fields:
        with engine().begin() as conn:
            conn.execute(update(sources).where(sources.c.id == source_id).values(**fields))


def delete_source(source_id: int):
    with engine().begin() as conn:
        conn.execute(delete(sources).where(sources.c.id == source_id))


# --- papers -----------------------------------------------------------------

def insert_paper(p: dict) -> bool:
    """Insert a newly collected paper. Returns False if it was already known."""
    ts = now_iso()
    values = dict(
        uid=p["uid"], source_kind=p["source_kind"], source_name=p.get("source_name"), title=p["title"],
        authors=json.dumps(p.get("authors", []), ensure_ascii=False), abstract=p.get("abstract"),
        published=p.get("published"), url=p.get("url"), pdf_url=p.get("pdf_url"), doi=p.get("doi"),
        categories=json.dumps(p.get("categories", [])),
        matched_keywords=json.dumps(p.get("matched_keywords", []), ensure_ascii=False),
        citation_count=p.get("citation_count"), status="new", card_paths="[]", created_at=ts, updated_at=ts,
        source_id=p.get("source_id"), venue=p.get("venue"),
    )
    with engine().begin() as conn:
        # rowcount is unreliable for ON CONFLICT on Postgres; RETURNING works on both.
        result = conn.execute(_insert(papers).values(**values).on_conflict_do_nothing().returning(papers.c.id))
        return result.first() is not None


def set_citation_count(paper_id: int, count: int):
    """Citation refreshes touch every reviewed paper each run; they are not edits,
    so updated_at stays as it was."""
    with engine().begin() as conn:
        conn.execute(update(papers).where(papers.c.id == paper_id).values(citation_count=count))


def tag_venue(uid: str, venue: str):
    """Note the journal/conference of a paper we already had (e.g. from an arXiv category)."""
    with engine().begin() as conn:
        conn.execute(update(papers).where(papers.c.uid == uid, papers.c.venue.is_(None)).values(venue=venue))


def source_filter(src: dict):
    """SQL condition for 'papers from this configured source'. Papers collected
    before sources were recorded are matched by their source label."""
    legacy = f"arXiv · {src['value']}" if src["kind"] == "arxiv" else src["name"]
    cond = (papers.c.source_id == src["id"]) | (papers.c.source_id.is_(None) & (papers.c.source_name == legacy))
    if src["kind"] == "journal":
        cond = cond | (papers.c.venue == src["name"])
    return cond


def ids_from_source(src: dict, paper_ids) -> set:
    """Which of these papers come from the source."""
    if not paper_ids:
        return set()
    with engine().connect() as conn:
        rows = conn.execute(select(papers.c.id).where(papers.c.id.in_(list(paper_ids)), source_filter(src))).all()
    return {r[0] for r in rows}


def source_counts():
    """{source_id: {status: n}} for every configured source."""
    out = {}
    with engine().connect() as conn:
        for src in list_sources():
            rows = conn.execute(select(papers.c.status, func.count()).where(source_filter(src)).group_by(papers.c.status)).all()
            out[src["id"]] = {status: n for status, n in rows}
    return out


def update_paper(paper_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = now_iso()
    for k in JSON_FIELDS:
        if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    with engine().begin() as conn:
        conn.execute(update(papers).where(papers.c.id == paper_id).values(**fields))


def get_paper(paper_id: int):
    with engine().connect() as conn:
        return _row(conn.execute(select(papers).where(papers.c.id == paper_id)).first())


def get_paper_by_uid(uid: str):
    with engine().connect() as conn:
        return _row(conn.execute(select(papers).where(papers.c.uid == uid)).first())


# newest review first; papers not reviewed yet fall back to when they were collected
_LATEST = func.coalesce(papers.c.reviewed_at, papers.c.created_at)

_ORDERS = {
    "recent": (_LATEST.desc(), papers.c.id.desc()),
    "score": (papers.c.score_overall.desc(), _LATEST.desc()),
    "relevance": (func.coalesce(papers.c.priority, 0).desc(), papers.c.relevance.desc(), papers.c.created_at.desc()),
}


def papers_by_status(status: str, limit: int = 1000, order: str = "recent", priority=None, source=None):
    q = select(papers).where(papers.c.status == status)
    if source:
        q = q.where(source_filter(source))
    if priority is True:
        q = q.where(func.coalesce(papers.c.priority, 0) > 0)
    elif priority is False:
        q = q.where(func.coalesce(papers.c.priority, 0) == 0)
    with engine().connect() as conn:
        return _rows(conn.execute(q.order_by(*_ORDERS[order]).limit(limit)))


def queue_papers(paper_ids, priority: int = 1):
    """Put papers (any status except done) at the front of the reading queue."""
    with engine().begin() as conn:
        result = conn.execute(update(papers).where(papers.c.id.in_(list(paper_ids)), papers.c.status != "done")
                              .values(status="screened", priority=priority, updated_at=now_iso()))
        return result.rowcount


def apply_threshold(min_relevance: float) -> dict:
    """Keep the queue in line with the current minimum relevance. Papers rated
    under it go to 'skipped' and ones at or above it come back to 'screened'.
    Hand-picked papers (priority > 0) are never moved."""
    auto = func.coalesce(papers.c.priority, 0) == 0
    with engine().begin() as conn:
        down = conn.execute(update(papers).where(papers.c.status == "screened", auto, papers.c.relevance.is_not(None),
                                                 papers.c.relevance < min_relevance)
                            .values(status="skipped", updated_at=now_iso())).rowcount
        up = conn.execute(update(papers).where(papers.c.status == "skipped", auto,
                                               papers.c.relevance >= min_relevance)
                          .values(status="screened", updated_at=now_iso())).rowcount
    return {"skipped": down, "screened": up}


def skip_papers(paper_ids) -> int:
    """Take papers out of the reading plan (they stay pickable under 관련도 미달).
    priority -1 marks them as set aside by hand, so apply_threshold leaves them there."""
    with engine().begin() as conn:
        return conn.execute(update(papers).where(papers.c.id.in_(list(paper_ids)), papers.c.status.in_(("screened", "new", "error")))
                            .values(status="skipped", priority=-1, updated_at=now_iso())).rowcount


def keyword_counts():
    """Reviewed papers per keyword, for the sidebar."""
    with engine().connect() as conn:
        rows = conn.execute(select(papers.c.matched_keywords).where(papers.c.status == "done")).all()
    counts = {}
    for (matched,) in rows:
        for kw in json.loads(matched or "[]"):
            counts[kw] = counts.get(kw, 0) + 1
    return counts


def query_papers(status="done", keyword=None, search=None, sort="recent", days=None, limit=60, offset=0, source=None):
    q = select(papers).where(papers.c.status == status)
    if source:
        q = q.where(source_filter(source))
    if keyword:
        q = q.where(papers.c.matched_keywords.like(f'%{json.dumps(keyword, ensure_ascii=False)}%'))
    if search:
        pat = f"%{search}%"
        q = q.where(papers.c.title.ilike(pat) | papers.c.abstract.ilike(pat) | papers.c.review.ilike(pat))
    if days:
        q = q.where(_LATEST >= ago_iso(days=int(days)))
    q = q.order_by(*_ORDERS.get(sort, _ORDERS["recent"])).limit(limit).offset(offset)
    with engine().connect() as conn:
        return _rows(conn.execute(q))


def paper_counts():
    with engine().connect() as conn:
        rows = conn.execute(select(papers.c.status, func.count()).group_by(papers.c.status)).all()
        today = conn.execute(select(func.count()).select_from(papers)
                             .where(papers.c.status == "done", _LATEST >= ago_iso(days=1))).scalar()
    counts = {status: n for status, n in rows}
    counts["today"] = today
    return counts


def keyword_activity(days: int = 14):
    """Collected-paper counts per matched keyword per day, for the trend chart."""
    with engine().connect() as conn:
        rows = conn.execute(select(papers.c.matched_keywords, papers.c.created_at)
                            .where(papers.c.created_at >= ago_iso(days=days))).all()
    out = {}
    for matched, created in rows:
        day = created[:10]
        for kw in json.loads(matched or "[]"):
            out.setdefault(kw, {}).setdefault(day, 0)
            out[kw][day] += 1
    return out


def cost_summary(days: int = 30):
    """Total API spend and reviewed-paper count over the last `days`."""
    with engine().connect() as conn:
        total, n = conn.execute(select(func.coalesce(func.sum(papers.c.cost_usd), 0.0), func.count(papers.c.cost_usd))
                                .where(papers.c.updated_at >= ago_iso(days=days))).one()
    return {"days": days, "total_usd": round(total or 0.0, 4), "papers": n,
            "per_paper_usd": round((total or 0.0) / n, 4) if n else None}


# --- runs -------------------------------------------------------------------

def start_run(trigger: str) -> int:
    with engine().begin() as conn:
        result = conn.execute(runs.insert().values(trigger=trigger, status="running", started_at=now_iso(), stats="{}", log=""))
        return result.inserted_primary_key[0]


def active_run(max_age_hours: float = 3):
    """A run still marked 'running' that started recently (any process/dyno)."""
    with engine().connect() as conn:
        return _row(conn.execute(select(runs).where(runs.c.status == "running", runs.c.started_at >= ago_iso(hours=max_age_hours))
                                 .order_by(runs.c.id.desc()).limit(1)).first())


def last_finished_run():
    with engine().connect() as conn:
        return _row(conn.execute(select(runs).where(runs.c.status == "ok").order_by(runs.c.id.desc()).limit(1)).first())


def append_run_log(run_id: int, line: str):
    with engine().begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id)
                     .values(log=runs.c.log.concat(f"[{datetime.now(config.TIMEZONE):%H:%M:%S}] {line}\n")))


def finish_run(run_id: int, status: str, stats: dict):
    """Record the outcome. A run the admin stopped keeps its 'aborted' status;
    only its stats are filled in."""
    with engine().begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "running")
                     .values(status=status, finished_at=now_iso(), stats=json.dumps(stats)))
        conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status != "running")
                     .values(stats=json.dumps(stats)))


def get_run(run_id: int):
    with engine().connect() as conn:
        row = conn.execute(select(runs).where(runs.c.id == run_id)).first()
    if not row:
        return None
    d = dict(row._mapping)
    d["stats"] = json.loads(d["stats"] or "{}")
    d["progress"] = json.loads(d["progress"] or "{}")
    return d


def run_status(run_id: int):
    with engine().connect() as conn:
        return conn.execute(select(runs.c.status).where(runs.c.id == run_id)).scalar()


def set_run_progress(run_id: int, progress: dict):
    with engine().begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(progress=json.dumps(progress, ensure_ascii=False)))


def abort_run(run_id: int, reason: str) -> bool:
    """Stop a run now, from the dashboard: it no longer counts as running, papers
    it was reading go back to the queue, and its thread (if it is still alive
    somewhere) stops at its next step. Returns False if it was not running."""
    with engine().begin() as conn:
        done = conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "running")
                            .values(status="aborted", finished_at=now_iso())).rowcount
        if done:
            conn.execute(update(papers).where(papers.c.status == "reading").values(status="screened"))
    if done:
        append_run_log(run_id, reason)
    return bool(done)


def recent_runs(limit=10):
    with engine().connect() as conn:
        rows = conn.execute(select(runs).order_by(runs.c.id.desc()).limit(limit)).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["stats"] = json.loads(d["stats"] or "{}")
        d["progress"] = json.loads(d.get("progress") or "{}")
        out.append(d)
    return out


def mark_stale_runs(max_age_hours: float = 3):
    """Runs and papers left in progress by a process that died. Only touches old
    ones, so a run happening in another dyno right now is left alone."""
    cutoff = ago_iso(hours=max_age_hours)
    with engine().begin() as conn:
        conn.execute(update(runs).where(runs.c.status == "running", runs.c.started_at < cutoff)
                     .values(status="aborted", finished_at=now_iso()))
        conn.execute(update(papers).where(papers.c.status == "reading", papers.c.updated_at < cutoff)
                     .values(status="screened"))


# --- trending & citations ---------------------------------------------------

def save_trending(kind: str, payload):
    stmt = _insert(trending_cache).values(kind=kind, payload=json.dumps(payload, ensure_ascii=False), updated_at=now_iso())
    with engine().begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["kind"],
                                                set_={"payload": stmt.excluded.payload, "updated_at": stmt.excluded.updated_at}))


def get_trending(kind: str):
    with engine().connect() as conn:
        row = conn.execute(select(trending_cache.c.payload, trending_cache.c.updated_at)
                           .where(trending_cache.c.kind == kind)).first()
    if not row:
        return None, None
    return json.loads(row.payload), row.updated_at


def snapshot_citations(uid: str, count: int):
    stmt = _insert(citation_snapshots).values(uid=uid, day=date.today().isoformat(), count=count)
    with engine().begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["uid", "day"], set_={"count": stmt.excluded.count}))


def citation_growth(uid: str, days: int = 7):
    """Citations gained over the last `days`, from our own daily snapshots."""
    since = (date.today() - timedelta(days=days)).isoformat()
    with engine().connect() as conn:
        rows = conn.execute(select(citation_snapshots.c.count)
                            .where(citation_snapshots.c.uid == uid, citation_snapshots.c.day >= since)
                            .order_by(citation_snapshots.c.day)).all()
    if len(rows) < 2:
        return None
    return rows[-1][0] - rows[0][0]


# --- blobs (used by storage.py when STORAGE_BACKEND=db) ---------------------

def put_blob(key: str, data: bytes, content_type: str):
    stmt = _insert(blobs).values(key=key, data=data, content_type=content_type, updated_at=now_iso())
    with engine().begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={
            "data": stmt.excluded.data, "content_type": stmt.excluded.content_type, "updated_at": stmt.excluded.updated_at}))


def blob_updated_at(key: str):
    with engine().connect() as conn:
        return conn.execute(select(blobs.c.updated_at).where(blobs.c.key == key)).scalar()


def get_blob(key: str):
    with engine().connect() as conn:
        row = conn.execute(select(blobs.c.data, blobs.c.content_type).where(blobs.c.key == key)).first()
    return (bytes(row.data), row.content_type) if row else None


def delete_blobs(prefix: str, exact: bool = False):
    with engine().begin() as conn:
        conn.execute(delete(blobs).where(blobs.c.key == prefix if exact else blobs.c.key.like(f"{prefix}%")))


# --- tech news ----------------------------------------------------------------

def insert_tech(item: dict) -> bool:
    """Insert a newly collected tech item. Returns False if it was already known."""
    ts = now_iso()
    values = dict(uid=item["uid"], source=item["source"], title=item["title"][:500], url=item.get("url"),
                  summary=item.get("summary"), meta=json.dumps(item.get("meta") or {}, ensure_ascii=False),
                  media=json.dumps(item.get("media") or {}, ensure_ascii=False), status="new", card_paths="[]",
                  created_at=ts, updated_at=ts)
    with engine().begin() as conn:
        stmt = _insert(tech_items).values(**values).on_conflict_do_nothing(index_elements=["uid"]).returning(tech_items.c.id)
        return conn.execute(stmt).first() is not None


def update_tech(item_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = now_iso()
    for k in JSON_FIELDS:
        if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    with engine().begin() as conn:
        conn.execute(update(tech_items).where(tech_items.c.id == item_id).values(**fields))


def get_tech(item_id: int):
    with engine().connect() as conn:
        return _row(conn.execute(select(tech_items).where(tech_items.c.id == item_id)).first())


def get_tech_by_uid(uid: str):
    with engine().connect() as conn:
        return _row(conn.execute(select(tech_items).where(tech_items.c.uid == uid)).first())


def tech_by_status(status: str, limit: int = 200, source=None):
    """Items in a state; screened ones best first, the rest newest first."""
    q = select(tech_items).where(tech_items.c.status == status)
    if source:
        q = q.where(tech_items.c.source == source)
    order = (tech_items.c.interest.desc().nulls_last(), tech_items.c.created_at.desc()) if status == "screened" \
        else (tech_items.c.created_at.desc(),)
    with engine().connect() as conn:
        return _rows(conn.execute(q.order_by(*order).limit(limit)))


def query_tech(source=None, search=None, limit=24, offset=0):
    """Finished tech posts, newest first."""
    q = select(tech_items).where(tech_items.c.status == "done")
    if source:
        q = q.where(tech_items.c.source == source)
    if search:
        pat = f"%{search}%"
        q = q.where(tech_items.c.title.ilike(pat) | tech_items.c.summary.ilike(pat) | tech_items.c.review.ilike(pat))
    q = q.order_by(func.coalesce(tech_items.c.reviewed_at, tech_items.c.updated_at).desc()).limit(limit).offset(offset)
    with engine().connect() as conn:
        return _rows(conn.execute(q))


def tech_counts():
    """{"status": {status: n}, "done_by_source": {source: n}}"""
    with engine().connect() as conn:
        by_status = dict(conn.execute(select(tech_items.c.status, func.count()).group_by(tech_items.c.status)).all())
        by_source = dict(conn.execute(select(tech_items.c.source, func.count()).where(tech_items.c.status == "done")
                                      .group_by(tech_items.c.source)).all())
    return {"status": by_status, "done_by_source": by_source}
