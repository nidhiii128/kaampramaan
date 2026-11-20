"""Public demo sandbox helpers (only used when KP_DEMO_MODE=1).

Every visitor gets a private sandbox worker, found through a random cookie, whose phone number starts with
'demo-'. The database keeps those workers out of the real dashboard, contractor scores and statistics, and
deletes them after 24 hours.
"""
import re
import secrets
import time
from collections import defaultdict, deque

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


def ensure_worker(db: Database, phone: str):
    """The visitor's worker, already registered at a sandbox site so the demo starts at the daily-use step."""
    worker = db.get_worker_by_phone(phone)
    if worker:
        return worker
    site = db.create_site(project="Demo site (sandbox)", principal_employer="Demo Developers Pvt Ltd",
                          employer_type="developer", contractor="Ramesh", location="Delhi NCR")
    db.create_worker(phone, name="Demo Worker", village="Demo Village", state="active", site_id=site)
    return db.get_worker_by_phone(phone)


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
