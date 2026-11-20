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
    kind TEXT NOT NULL CHECK (kind IN ('work', 'payment', 'void')),
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
    void_of INTEGER,                   -- for kind 'void': seq of the entry this cancels
    UNIQUE (worker_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_entries_site_date ON entries (site_id, work_date);

-- kind 'death_report' (family reports) and 'wage_complaint' (generated -> filed -> resolved / closed)
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    reporter_phone TEXT,
    worker_id INTEGER REFERENCES workers(id),
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    amount_claimed INTEGER,
    amount_recovered INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
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


WAGE_CASE_STATUSES = ("generated", "filed", "resolved", "closed")
DEMO_PREFIX = "demo-"          # phone numbers of public-demo visitors; kept out of real data and statistics
_NOT_DEMO = "coalesce(w.phone, '') NOT LIKE 'demo-%'"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self.connect() as conn:
            old = self._old_entries_table(conn)
            if old:
                conn.execute("DROP INDEX IF EXISTS idx_entries_site_date")
                conn.execute("ALTER TABLE entries RENAME TO entries_old")
            conn.executescript(SCHEMA)
            self._add_missing_case_columns(conn)
            if old:  # databases created before 'void' entries existed: copy rows over, hashes stay valid
                cols = ("id, worker_id, seq, kind, work_date, site_id, contractor, amount, paid, source, transcript, "
                        "media_path, media_sha256, latitude, longitude, recorded_at, prev_hash, hash")
                conn.execute(f"INSERT INTO entries ({cols}) SELECT {cols} FROM entries_old")
                conn.execute("DROP TABLE entries_old")

    @staticmethod
    def _add_missing_case_columns(conn):
        have = {r["name"] for r in conn.execute("PRAGMA table_info(cases)")}
        for col, ddl in (("amount_claimed", "INTEGER"), ("amount_recovered", "INTEGER NOT NULL DEFAULT 0"),
                         ("updated_at", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {ddl}")

    @staticmethod
    def _old_entries_table(conn) -> bool:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entries'").fetchone()
        return bool(row) and "void_of" not in row["sql"]

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
                   WHERE coalesce(w.phone, '') NOT LIKE 'demo-%'
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
        """For each work entry, how many *other* workers logged the same site and date.
        Entries that were cancelled with a 'void' entry do not count."""
        with self.connect() as c:
            rows = c.execute(
                """SELECT e.id, COUNT(DISTINCT o.worker_id) AS n
                   FROM entries e
                   LEFT JOIN entries o
                     ON o.kind = 'work' AND o.work_date = e.work_date
                    AND o.site_id = e.site_id AND o.worker_id != e.worker_id
                    AND NOT EXISTS (SELECT 1 FROM entries v WHERE v.worker_id = o.worker_id
                                    AND v.kind = 'void' AND v.void_of = o.seq)
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
        """Reports from families (death reports etc). Wage complaints have their own list."""
        with self.connect() as c:
            return c.execute(
                """SELECT c.*, w.name AS worker_name FROM cases c
                   LEFT JOIN workers w ON w.id = c.worker_id
                   WHERE c.kind != 'wage_complaint' AND coalesce(c.reporter_phone, '') NOT LIKE 'demo-%'
                   ORDER BY c.created_at DESC"""
            ).fetchall()

    # ---- wage complaints: generated -> filed -> resolved / closed ----
    def open_wage_case(self, worker_id: int, claimed: int, details: str | None = None) -> int:
        """One live complaint per worker: asking again refreshes the claim instead of opening a duplicate."""
        with self.connect() as c:
            row = c.execute(
                "SELECT id FROM cases WHERE kind = 'wage_complaint' AND worker_id = ? "
                "AND status IN ('generated', 'filed') ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
            if row:
                c.execute("UPDATE cases SET amount_claimed = ?, details = ?, updated_at = ? WHERE id = ?",
                          (claimed, details, now_iso(), row["id"]))
                return row["id"]
            cur = c.execute(
                "INSERT INTO cases (kind, worker_id, details, status, amount_claimed, amount_recovered, "
                "created_at, updated_at) VALUES ('wage_complaint', ?, ?, 'generated', ?, 0, ?, ?)",
                (worker_id, details, claimed, now_iso(), now_iso()))
            return cur.lastrowid

    def get_case(self, case_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()

    def update_case(self, case_id: int, status: str, amount_recovered: int):
        if status not in WAGE_CASE_STATUSES:
            raise ValueError(f"status must be one of {WAGE_CASE_STATUSES}")
        with self.connect() as c:
            c.execute("UPDATE cases SET status = ?, amount_recovered = ?, updated_at = ? WHERE id = ?",
                      (status, amount_recovered, now_iso(), case_id))

    def list_wage_cases(self):
        with self.connect() as c:
            return c.execute(
                f"""SELECT c.*, w.name AS worker_name, w.phone AS worker_phone, w.village AS worker_village
                    FROM cases c JOIN workers w ON w.id = c.worker_id
                    WHERE c.kind = 'wage_complaint' AND {_NOT_DEMO}
                    ORDER BY CASE c.status WHEN 'generated' THEN 0 WHEN 'filed' THEN 1 ELSE 2 END, c.id DESC"""
            ).fetchall()

    def impact_stats(self) -> dict:
        """What the project has achieved: complaints by stage, wages claimed and wages recovered."""
        rows = self.list_wage_cases()
        by = {s: 0 for s in WAGE_CASE_STATUSES}
        for r in rows:
            by[r["status"]] += 1
        claimed = sum(r["amount_claimed"] or 0 for r in rows)
        recovered = sum(r["amount_recovered"] or 0 for r in rows)
        return {"complaints": len(rows), "by_status": by, "claimed": claimed, "recovered": recovered,
                "recovery_rate": round(100 * recovered / claimed) if claimed else 0}

    # ---- public demo sandbox ----
    def purge_demo_worker(self, phone: str):
        """Delete one demo visitor's sandbox data. Only ever touches phones that start with 'demo-'."""
        if not phone.startswith(DEMO_PREFIX):
            raise ValueError("not a demo worker")
        with self.connect() as c:
            row = c.execute("SELECT id, site_id FROM workers WHERE phone = ?", (phone,)).fetchone()
            if not row:
                return
            wid = row["id"]
            for table in ("recommendations", "documents", "cases", "entries"):
                c.execute(f"DELETE FROM {table} WHERE worker_id = ?", (wid,))
            c.execute("DELETE FROM workers WHERE id = ?", (wid,))
            if row["site_id"]:
                c.execute("DELETE FROM sites WHERE id = ? AND id NOT IN (SELECT site_id FROM workers "
                          "WHERE site_id IS NOT NULL)", (row["site_id"],))

    def purge_stale_demo(self, hours: int = 24) -> int:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self.connect() as c:
            phones = [r["phone"] for r in c.execute(
                "SELECT phone FROM workers WHERE phone LIKE 'demo-%' AND created_at < ?", (cutoff,))]
        for phone in phones:
            self.purge_demo_worker(phone)
        return len(phones)

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
        """Every real worker's entries, oldest first. Used for contractor payment records.
        Public-demo visitors are left out, so a visitor cannot change a real contractor's score."""
        with self.connect() as c:
            return c.execute(
                f"SELECT e.* FROM entries e JOIN workers w ON w.id = e.worker_id WHERE {_NOT_DEMO} "
                "ORDER BY e.worker_id, e.seq").fetchall()

    # ---- webhook de-duplication (Meta retries deliveries) ----
    def mark_processed(self, message_id: str) -> bool:
        """Returns False if this message was already handled."""
        with self.connect() as c:
            try:
                c.execute("INSERT INTO processed_messages VALUES (?, ?)", (message_id, now_iso()))
                return True
            except sqlite3.IntegrityError:
                return False
