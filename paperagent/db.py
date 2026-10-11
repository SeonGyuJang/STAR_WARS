"""SQLite storage. One short-lived connection per call keeps it safe across the
Flask request threads and the background scheduler thread."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);

-- kind: 'arxiv' (value = arXiv category, e.g. cs.CL) or
--       'journal' (value = OpenAlex source id, e.g. S4306420609)
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    source_name TEXT,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]',
    abstract TEXT,
    published TEXT,
    url TEXT,
    pdf_url TEXT,
    doi TEXT,
    categories TEXT NOT NULL DEFAULT '[]',
    matched_keywords TEXT NOT NULL DEFAULT '[]',
    relevance REAL,
    relevance_reason TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    read_depth TEXT,
    local_pdf TEXT,
    drive_link TEXT,
    review TEXT,
    score_method REAL,
    score_novelty REAL,
    score_overall REAL,
    card_paths TEXT NOT NULL DEFAULT '[]',
    citation_count INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_created ON papers(created_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats TEXT NOT NULL DEFAULT '{}',
    log TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citation_snapshots (
    uid TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (uid, day)
);

CREATE TABLE IF NOT EXISTS trending_cache (
    kind TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS:
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except json.JSONDecodeError:
                pass
    return d


def init_db(seed: bool = True):
    with connect() as conn:
        conn.executescript(SCHEMA)
        if not seed:
            return
        if conn.execute("SELECT COUNT(*) FROM settings WHERE key='seeded'").fetchone()[0]:
            return
        ts = now_iso()
        conn.executemany(
            "INSERT OR IGNORE INTO keywords(term, created_at) VALUES (?, ?)",
            [(k, ts) for k in DEFAULT_KEYWORDS],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO sources(kind, value, name, created_at) VALUES ('arxiv', ?, ?, ?)",
            [(v, n, ts) for v, n in DEFAULT_ARXIV],
        )
        # Unresolved journals are stored with value 'name:<journal name>' and
        # replaced with their OpenAlex id the first time the pipeline runs.
        conn.executemany(
            "INSERT OR IGNORE INTO sources(kind, value, name, created_at) VALUES ('journal', ?, ?, ?)",
            [(f"name:{n}", n, ts) for n in DEFAULT_JOURNALS],
        )
        conn.execute("INSERT INTO settings(key, value) VALUES ('seeded', '1')")


# --- settings ---------------------------------------------------------------

def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


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
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM keywords ORDER BY term")]


def add_keyword(term: str) -> bool:
    term = term.strip()
    if not term:
        return False
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO keywords(term, created_at) VALUES (?, ?)", (term, now_iso()))
        return cur.rowcount > 0


def delete_keyword(keyword_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM keywords WHERE id=?", (keyword_id,))


def list_sources(kind=None, enabled_only=False):
    q = "SELECT * FROM sources WHERE 1=1"
    args = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if enabled_only:
        q += " AND enabled=1"
    q += " ORDER BY kind, name"
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def add_source(kind: str, value: str, name: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sources(kind, value, name, created_at) VALUES (?, ?, ?, ?)",
            (kind, value, name, now_iso()),
        )
        return cur.rowcount > 0


def update_source(source_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), source_id))


def delete_source(source_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


# --- papers -----------------------------------------------------------------

def insert_paper(p: dict) -> bool:
    """Insert a newly collected paper. Returns False if it was already known."""
    ts = now_iso()
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO papers
               (uid, source_kind, source_name, title, authors, abstract, published, url, pdf_url, doi,
                categories, matched_keywords, citation_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                p["uid"], p["source_kind"], p.get("source_name"), p["title"],
                json.dumps(p.get("authors", []), ensure_ascii=False), p.get("abstract"),
                p.get("published"), p.get("url"), p.get("pdf_url"), p.get("doi"),
                json.dumps(p.get("categories", [])),
                json.dumps(p.get("matched_keywords", []), ensure_ascii=False),
                p.get("citation_count"), ts, ts,
            ),
        )
        return cur.rowcount > 0


def update_paper(paper_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = now_iso()
    vals = []
    for k, v in fields.items():
        vals.append(json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and not isinstance(v, str) else v)
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE papers SET {cols} WHERE id=?", (*vals, paper_id))


def get_paper(paper_id: int):
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())


def papers_by_status(status: str, limit: int = 1000, order: str = "created_at DESC"):
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM papers WHERE status=? ORDER BY {order} LIMIT ?", (status, limit))
        return [row_to_dict(r) for r in rows]


def query_papers(status="done", keyword=None, search=None, sort="recent", days=None, limit=60, offset=0):
    q = "SELECT * FROM papers WHERE status=?"
    args = [status]
    if keyword:
        q += " AND matched_keywords LIKE ?"
        args.append(f'%"{keyword}"%')
    if search:
        q += " AND (title LIKE ? OR abstract LIKE ? OR review LIKE ?)"
        args += [f"%{search}%"] * 3
    if days:
        q += " AND created_at >= datetime('now', ?)"
        args.append(f"-{int(days)} days")
    q += {
        "score": " ORDER BY score_overall DESC, created_at DESC",
        "relevance": " ORDER BY relevance DESC, created_at DESC",
    }.get(sort, " ORDER BY created_at DESC")
    q += " LIMIT ? OFFSET ?"
    args += [limit, offset]
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(q, args)]


def paper_counts():
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM papers GROUP BY status").fetchall()
        today = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE status='done' AND updated_at >= datetime('now','-1 day')"
        ).fetchone()[0]
    counts = {r["status"]: r["n"] for r in rows}
    counts["today"] = today
    return counts


def keyword_activity(days: int = 14):
    """Collected-paper counts per matched keyword per day, for the trend chart."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT matched_keywords, substr(created_at, 1, 10) AS day FROM papers WHERE created_at >= datetime('now', ?)",
            (f"-{days} days",),
        ).fetchall()
    out = {}
    for r in rows:
        for kw in json.loads(r["matched_keywords"] or "[]"):
            out.setdefault(kw, {}).setdefault(r["day"], 0)
            out[kw][r["day"]] += 1
    return out


# --- runs -------------------------------------------------------------------

def start_run(trigger: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(trigger, status, started_at) VALUES (?, 'running', ?)", (trigger, now_iso())
        )
        return cur.lastrowid


def append_run_log(run_id: int, line: str):
    with connect() as conn:
        conn.execute("UPDATE runs SET log = log || ? WHERE id=?", (f"[{now_iso()[11:19]}] {line}\n", run_id))


def finish_run(run_id: int, status: str, stats: dict):
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, stats=? WHERE id=?",
            (status, now_iso(), json.dumps(stats), run_id),
        )


def recent_runs(limit=10):
    with connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = json.loads(d["stats"] or "{}")
        out.append(d)
    return out


def mark_stale_runs():
    """Runs left 'running' by a crashed/restarted process."""
    with connect() as conn:
        conn.execute("UPDATE runs SET status='aborted', finished_at=? WHERE status='running'", (now_iso(),))
        conn.execute("UPDATE papers SET status='screened' WHERE status='reading'")


# --- trending & citations ---------------------------------------------------

def save_trending(kind: str, payload):
    with connect() as conn:
        conn.execute(
            """INSERT INTO trending_cache(kind, payload, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
            (kind, json.dumps(payload, ensure_ascii=False), now_iso()),
        )


def get_trending(kind: str):
    with connect() as conn:
        row = conn.execute("SELECT payload, updated_at FROM trending_cache WHERE kind=?", (kind,)).fetchone()
    if not row:
        return None, None
    return json.loads(row["payload"]), row["updated_at"]


def snapshot_citations(uid: str, count: int):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO citation_snapshots(uid, day, count) VALUES (?, date('now'), ?)", (uid, count)
        )


def citation_growth(uid: str, days: int = 7):
    """Citations gained over the last `days`, from our own daily snapshots."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT day, count FROM citation_snapshots WHERE uid=? AND day >= date('now', ?) ORDER BY day",
            (uid, f"-{days} days"),
        ).fetchall()
    if len(rows) < 2:
        return None
    return rows[-1]["count"] - rows[0]["count"]
