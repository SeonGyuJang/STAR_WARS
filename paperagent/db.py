"""Storage for papers, settings, runs and blobs.

SQLAlchemy Core keeps one code path for SQLite (local runs) and Postgres
(Heroku, where the filesystem is wiped on every dyno restart). Timestamps are
stored as ISO-8601 UTC strings and compared as strings, which keeps queries
free of dialect-specific date functions."""
import json
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
#       'journal' (value = OpenAlex source id, e.g. S4306420609, or 'name:<journal>' until resolved)
sources = Table(
    "sources", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(20), nullable=False),
    Column("value", String(300), nullable=False),
    Column("name", String(300), nullable=False),
    Column("enabled", Integer, nullable=False, default=1),
    Column("created_at", String(32), nullable=False),
    UniqueConstraint("kind", "value"),
)

papers = Table(
    "papers", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(300), nullable=False, unique=True),
    Column("source_kind", String(20), nullable=False),
    Column("source_name", String(300)),
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
    # > 0 when an admin picked the paper by hand (read first, even if below the relevance threshold)
    Column("priority", Integer, default=0),
    Column("card_paths", Text, nullable=False, default="[]"),
    Column("citation_count", Integer),
    Column("error", Text),
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

JSON_FIELDS = ("authors", "categories", "matched_keywords", "review", "card_paths")

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


def init_db(seed: bool = True):
    metadata.create_all(engine())
    _add_missing_columns()
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


def add_source(kind: str, value: str, name: str) -> bool:
    with engine().begin() as conn:
        result = conn.execute(_insert(sources).values(kind=kind, value=value, name=name, enabled=1, created_at=now_iso())
                              .on_conflict_do_nothing().returning(sources.c.id))
        return result.first() is not None


def update_source(source_id: int, **fields):
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
    )
    with engine().begin() as conn:
        # rowcount is unreliable for ON CONFLICT on Postgres; RETURNING works on both.
        result = conn.execute(_insert(papers).values(**values).on_conflict_do_nothing().returning(papers.c.id))
        return result.first() is not None


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


_ORDERS = {
    "recent": (papers.c.created_at.desc(),),
    "score": (papers.c.score_overall.desc(), papers.c.created_at.desc()),
    "relevance": (func.coalesce(papers.c.priority, 0).desc(), papers.c.relevance.desc(), papers.c.created_at.desc()),
}


def papers_by_status(status: str, limit: int = 1000, order: str = "recent", priority=None):
    q = select(papers).where(papers.c.status == status)
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


def keyword_counts():
    """Reviewed papers per keyword, for the sidebar."""
    with engine().connect() as conn:
        rows = conn.execute(select(papers.c.matched_keywords).where(papers.c.status == "done")).all()
    counts = {}
    for (matched,) in rows:
        for kw in json.loads(matched or "[]"):
            counts[kw] = counts.get(kw, 0) + 1
    return counts


def query_papers(status="done", keyword=None, search=None, sort="recent", days=None, limit=60, offset=0):
    q = select(papers).where(papers.c.status == status)
    if keyword:
        q = q.where(papers.c.matched_keywords.like(f'%{json.dumps(keyword, ensure_ascii=False)}%'))
    if search:
        pat = f"%{search}%"
        q = q.where(papers.c.title.ilike(pat) | papers.c.abstract.ilike(pat) | papers.c.review.ilike(pat))
    if days:
        q = q.where(papers.c.created_at >= ago_iso(days=int(days)))
    q = q.order_by(*_ORDERS.get(sort, _ORDERS["recent"])).limit(limit).offset(offset)
    with engine().connect() as conn:
        return _rows(conn.execute(q))


def paper_counts():
    with engine().connect() as conn:
        rows = conn.execute(select(papers.c.status, func.count()).group_by(papers.c.status)).all()
        today = conn.execute(select(func.count()).select_from(papers)
                             .where(papers.c.status == "done", papers.c.updated_at >= ago_iso(days=1))).scalar()
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
    with engine().begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id)
                     .values(status=status, finished_at=now_iso(), stats=json.dumps(stats)))


def recent_runs(limit=10):
    with engine().connect() as conn:
        rows = conn.execute(select(runs).order_by(runs.c.id.desc()).limit(limit)).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["stats"] = json.loads(d["stats"] or "{}")
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


def get_blob(key: str):
    with engine().connect() as conn:
        row = conn.execute(select(blobs.c.data, blobs.c.content_type).where(blobs.c.key == key)).first()
    return (bytes(row.data), row.content_type) if row else None


def delete_blobs(prefix: str):
    with engine().begin() as conn:
        conn.execute(delete(blobs).where(blobs.c.key.like(f"{prefix}%")))
