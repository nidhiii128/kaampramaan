"""Public demo sandbox helpers (only used when KP_DEMO_MODE=1).

Every visitor gets a private sandbox worker, found through a random cookie, whose phone number starts with
'demo-'. The database keeps those workers out of the real dashboard, contractor scores and statistics, and
deletes them after 24 hours.
"""
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import date, timedelta

from . import attest, ledger
from .db import DEMO_PREFIX, Database

COOKIE = "kp_demo"
VISITOR_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_UPLOAD = 3 * 1024 * 1024


def new_visitor_id() -> str:
    return secrets.token_hex(6)


def demo_phone(visitor_id: str) -> str:
    if not VISITOR_RE.match(visitor_id):
        raise ValueError("bad visitor id")
    return f"{DEMO_PREFIX}{visitor_id}"


def supervisor_phone(worker_phone: str) -> str:
    """The sandbox site's supervisor. Starts with 'demo-' too, and is never a worker."""
    return f"{worker_phone}-sup"


def ensure_worker(db: Database, phone: str):
    """The visitor's worker, already registered at a sandbox site so the demo starts at the daily-use step."""
    worker = db.get_worker_by_phone(phone)
    if worker:
        site = db.get_site(worker["site_id"])
        if site and not site["supervisor_phone"]:       # sandboxes made before supervisors existed
            db.set_supervisor(site["id"], "Demo Supervisor", supervisor_phone(phone))
        return worker
    site = db.create_site(project="Demo site (sandbox)", principal_employer="Demo Developers Pvt Ltd",
                          employer_type="developer", contractor="Ramesh", location="Delhi NCR",
                          supervisor_name="Demo Supervisor", supervisor_phone=supervisor_phone(phone))
    db.create_worker(phone, name="Demo Worker", village="Demo Village", state="active", site_id=site)
    return db.get_worker_by_phone(phone)


def add_confirmed_history(db: Database, worker, today: date, days: int = 89) -> int:
    """Fast-forward: `days` past working days (Sundays off) checked in at the sandbox site and confirmed by its
    supervisor, so a visitor can reach the 90-day rule with one more check-in today."""
    site = db.get_site(worker["site_id"])
    have = {e["work_date"] for e in db.list_entries(worker["id"]) if e["kind"] == "work"}
    d, made = today - timedelta(days=1), []
    while len(made) < days and d > today - timedelta(days=330):
        if d.weekday() != 6 and d.isoformat() not in have:
            made.append(ledger.append(db, ledger.NewEntry(
                worker_id=worker["id"], kind="work", work_date=d.isoformat(), amount=0, source="site_qr",
                site_id=site["id"], contractor=site["contractor"],
                transcript=f"Checked in at {site['project']} (site code {site['code']}, sandbox history)")))
        d -= timedelta(days=1)
    attest.attest(db, made, "supervisor", f"{site['supervisor_name'] or 'Demo Supervisor'} (sandbox)")
    return len(made)


class RateLimiter:
    """Sliding window per key (an IP address). In memory, so it resets on restart: enough to stop a loop
    from running up the Gemini bill, not a security boundary."""

    def __init__(self, limit: int, window_seconds: int, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window_seconds, clock
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.clock()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
