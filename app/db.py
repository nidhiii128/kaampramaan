"""SQLite storage. One connection per call keeps it safe under FastAPI's thread pool."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    project TEXT,
    principal_employer TEXT,
    employer_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (employer_type IN ('government', 'developer', 'small', 'unknown')),
    contractor TEXT,
    location TEXT,
    board_photo TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY,
    phone TEXT UNIQUE,                 -- NULL for workers logged by a volunteer
    name TEXT,
    village TEXT,
    site_id INTEGER REFERENCES sites(id),
    state TEXT NOT NULL DEFAULT 'new',
    last_wage INTEGER,
    last_lat REAL,
    last_lon REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('work', 'payment')),
    work_date TEXT NOT NULL,
    site_id INTEGER REFERENCES sites(id),
    contractor TEXT,
    amount INTEGER NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    transcript TEXT,
    media_path TEXT,
    media_sha256 TEXT,
    latitude REAL,
    longitude REAL,
    recorded_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    UNIQUE (worker_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_entries_site_date ON entries (site_id, work_date);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    reporter_phone TEXT,
    worker_id INTEGER REFERENCES workers(id),
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    token TEXT PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- Work openings posted by NGOs, unions or site supervisors, matched to workers by app/recommend.py
CREATE TABLE IF NOT EXISTS openings (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT,
    contractor TEXT,
    site_id INTEGER REFERENCES sites(id),
    location TEXT,
    latitude REAL,
    longitude REAL,
    wage INTEGER NOT NULL CHECK (wage > 0),
    slots INTEGER NOT NULL DEFAULT 1 CHECK (slots > 0),
    contact TEXT,
    starts_on TEXT NOT NULL,
    ends_on TEXT,
    posted_by TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_openings_active ON openings (active, ends_on);

-- Embedding cache: an opening's text is embedded once per model, not on every search
CREATE TABLE IF NOT EXISTS embeddings (
    model TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    vector TEXT NOT NULL,              -- JSON list of floats
    PRIMARY KEY (model, text_sha256)
);

-- What each worker was shown, in which order, so ranking can be evaluated later
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    opening_id INTEGER NOT NULL REFERENCES openings(id),
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    query TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recommendations_worker ON recommendations (worker_id, created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- workers ----
    def get_worker_by_phone(self, phone: str):
        with self.connect() as c:
            return c.execute("SELECT * FROM workers WHERE phone = ?", (phone,)).fetchone()

    def get_worker(self, worker_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()

    def create_worker(self, phone, name=None, village=None, state="new", site_id=None) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO workers (phone, name, village, state, site_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (phone, name, village, state, site_id, now_iso()),
            )
            return cur.lastrowid

    def update_worker(self, worker_id: int, **fields):
        allowed = {"name", "village", "site_id", "state", "last_wage", "last_lat", "last_lon"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown worker fields: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE workers SET {cols} WHERE id = ?", (*fields.values(), worker_id))

    def list_workers(self):
        with self.connect() as c:
            return c.execute(
                """SELECT w.*, s.project, s.principal_employer, s.employer_type
                   FROM workers w LEFT JOIN sites s ON s.id = w.site_id
                   ORDER BY w.created_at DESC"""
            ).fetchall()

    # ---- sites ----
    def create_site(self, project=None, principal_employer=None, employer_type="unknown",
                    contractor=None, location=None, board_photo=None) -> int:
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO sites (project, principal_employer, employer_type, contractor,
                   location, board_photo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project, principal_employer, employer_type, contractor, location, board_photo, now_iso()),
            )
            return cur.lastrowid

    def get_site(self, site_id):
        if site_id is None:
            return None
        with self.connect() as c:
            return c.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()

    def find_site(self, project: str, contractor: str | None):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM sites WHERE lower(project) = lower(?) AND lower(coalesce(contractor,'')) = lower(?)",
                (project, contractor or ""),
            ).fetchone()

    # ---- entries ----
    def list_entries(self, worker_id: int):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM entries WHERE worker_id = ? ORDER BY seq", (worker_id,)
            ).fetchall()

    def corroboration_counts(self, worker_id: int) -> dict[int, int]:
        """For each work entry, how many *other* workers logged the same site and date."""
        with self.connect() as c:
            rows = c.execute(
                """SELECT e.id, COUNT(DISTINCT o.worker_id) AS n
                   FROM entries e
                   LEFT JOIN entries o
                     ON o.kind = 'work' AND o.work_date = e.work_date
                    AND o.site_id = e.site_id AND o.worker_id != e.worker_id
                   WHERE e.worker_id = ? AND e.kind = 'work' AND e.site_id IS NOT NULL
                   GROUP BY e.id""",
                (worker_id,),
            ).fetchall()
        return {r["id"]: r["n"] for r in rows}

    # ---- cases and documents ----
    def create_case(self, kind, reporter_phone=None, worker_id=None, details=None) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO cases (kind, reporter_phone, worker_id, details, created_at) VALUES (?, ?, ?, ?, ?)",
                (kind, reporter_phone, worker_id, details, now_iso()),
            )
            return cur.lastrowid

    def list_cases(self):
        with self.connect() as c:
            return c.execute(
                """SELECT c.*, w.name AS worker_name FROM cases c
                   LEFT JOIN workers w ON w.id = c.worker_id ORDER BY c.created_at DESC"""
            ).fetchall()

    def save_document(self, token, worker_id, kind, path):
        with self.connect() as c:
            c.execute(
                "INSERT INTO documents (token, worker_id, kind, path, created_at) VALUES (?, ?, ?, ?, ?)",
                (token, worker_id, kind, path, now_iso()),
            )

    def get_document(self, token):
        with self.connect() as c:
            return c.execute("SELECT * FROM documents WHERE token = ?", (token,)).fetchone()

    # ---- work openings ----
    def create_opening(self, title, wage, starts_on, posted_by, details=None, contractor=None, site_id=None,
                       location=None, latitude=None, longitude=None, slots=1, contact=None, ends_on=None) -> int:
        with self.connect() as c:
            cur = c.execute(
                """INSERT INTO openings (title, details, contractor, site_id, location, latitude, longitude, wage,
                   slots, contact, starts_on, ends_on, posted_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, details, contractor, site_id, location, latitude, longitude, wage, slots, contact,
                 starts_on, ends_on, posted_by, now_iso()),
            )
            return cur.lastrowid

    def list_open_openings(self, today: str):
        """Active openings that have not ended yet."""
        with self.connect() as c:
            return c.execute(
                """SELECT * FROM openings WHERE active = 1 AND (ends_on IS NULL OR ends_on >= ?)
                   ORDER BY starts_on, id""", (today,)
            ).fetchall()

    def list_openings(self):
        with self.connect() as c:
            return c.execute(
                """SELECT o.*, COUNT(r.id) AS times_shown FROM openings o
                   LEFT JOIN recommendations r ON r.opening_id = o.id
                   GROUP BY o.id ORDER BY o.active DESC, o.created_at DESC"""
            ).fetchall()

    def close_opening(self, opening_id: int):
        with self.connect() as c:
            c.execute("UPDATE openings SET active = 0 WHERE id = ?", (opening_id,))

    def get_embeddings(self, model: str, hashes: list[str]) -> dict[str, str]:
        if not hashes:
            return {}
        with self.connect() as c:
            rows = c.execute(
                f"SELECT text_sha256, vector FROM embeddings WHERE model = ? AND text_sha256 IN "
                f"({', '.join('?' for _ in hashes)})", (model, *hashes)
            ).fetchall()
        return {r["text_sha256"]: r["vector"] for r in rows}

    def put_embeddings(self, model: str, items: dict[str, str]):
        with self.connect() as c:
            c.executemany("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?)",
                          [(model, h, v) for h, v in items.items()])

    def log_recommendations(self, worker_id: int, query: str | None, ranked: list[tuple[int, float]]):
        with self.connect() as c:
            c.executemany(
                "INSERT INTO recommendations (worker_id, opening_id, rank, score, query, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(worker_id, oid, i, round(score, 4), query, now_iso()) for i, (oid, score) in enumerate(ranked, 1)],
            )

    def all_work_entries(self):
        """Every entry from every worker, oldest first. Used for contractor payment records."""
        with self.connect() as c:
            return c.execute("SELECT * FROM entries ORDER BY worker_id, seq").fetchall()

    # ---- webhook de-duplication (Meta retries deliveries) ----
    def mark_processed(self, message_id: str) -> bool:
        """Returns False if this message was already handled."""
        with self.connect() as c:
            try:
                c.execute("INSERT INTO processed_messages VALUES (?, ?)", (message_id, now_iso()))
                return True
            except sqlite3.IntegrityError:
                return False
