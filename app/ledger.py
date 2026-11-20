"""Append-only, hash-chained work ledger.

Every entry stores the hash of the previous entry for the same worker. Changing,
deleting or reordering any past entry breaks the chain, and verify_chain() reports
the first entry where it breaks. Media files are fingerprinted with SHA-256 so a
swapped photo or voice note is also detected.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database, now_iso

GENESIS = "0" * 64

HASHED_FIELDS = (
    "worker_id", "seq", "kind", "work_date", "site_id", "contractor", "amount", "paid",
    "source", "transcript", "media_sha256", "latitude", "longitude", "recorded_at", "prev_hash",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_hash(fields: dict) -> str:
    data = {k: fields.get(k) for k in HASHED_FIELDS}
    # Only present on 'void' entries, so hashes of older entries are unchanged.
    if fields.get("void_of") is not None:
        data["void_of"] = fields["void_of"]
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class NewEntry:
    worker_id: int
    kind: str                 # 'work', 'payment', or 'void' (cancels the entry whose seq is void_of)
    work_date: str            # ISO date
    amount: int               # wage for 'work', amount received for 'payment'
    source: str               # 'whatsapp', 'volunteer', 'ivr', 'simulator'
    site_id: int | None = None
    contractor: str | None = None
    paid: bool = False        # for 'work': was this day's wage paid on the spot
    transcript: str | None = None
    media_path: str | None = None
    media_sha256: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    void_of: int | None = None


def _insert(c, e: NewEntry) -> dict:
    """Append one entry inside an open write transaction."""
    last = c.execute(
        "SELECT seq, hash FROM entries WHERE worker_id = ? ORDER BY seq DESC LIMIT 1",
        (e.worker_id,),
    ).fetchone()
    fields = {
        "worker_id": e.worker_id,
        "seq": (last["seq"] + 1) if last else 1,
        "kind": e.kind,
        "work_date": e.work_date,
        "site_id": e.site_id,
        "contractor": e.contractor,
        "amount": int(e.amount),
        "paid": 1 if e.paid else 0,
        "source": e.source,
        "transcript": e.transcript,
        "media_path": e.media_path,
        "media_sha256": e.media_sha256,
        "latitude": e.latitude,
        "longitude": e.longitude,
        "recorded_at": now_iso(),
        "prev_hash": last["hash"] if last else GENESIS,
        "void_of": e.void_of,
    }
    fields["hash"] = entry_hash(fields)
    cols = ", ".join(fields)
    c.execute(f"INSERT INTO entries ({cols}) VALUES ({', '.join('?' for _ in fields)})",
              tuple(fields.values()))
    return fields


def append(db: Database, e: NewEntry) -> dict:
    if e.kind not in ("work", "payment"):
        raise ValueError("kind must be 'work' or 'payment' (use void_last to cancel an entry)")
    if e.amount is None or e.amount < 0:
        raise ValueError("amount must be a non-negative number")
    with db.connect() as c:
        # BEGIN IMMEDIATE takes the write lock now, so two messages from the same
        # worker arriving together cannot both read the same last seq.
        c.execute("BEGIN IMMEDIATE")
        return _insert(c, e)


def void_last(db: Database, worker_id: int, source: str, transcript: str | None = None,
              max_age_hours: int | None = 24, now: datetime | None = None) -> tuple[dict, dict] | None:
    """Cancel the worker's most recent live (not yet cancelled) work or payment entry.

    Nothing is edited or deleted: a new 'void' entry pointing at the old one is appended, so the
    hash chain stays valid and the cancellation is itself on the record. Only entries recorded
    within max_age_hours can be cancelled this way, so repeating "galat" cannot erase history.
    Returns (cancelled entry, void entry), or None if there is nothing that can be cancelled.
    """
    now = now or datetime.now(timezone.utc)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute("SELECT * FROM entries WHERE worker_id = ? ORDER BY seq", (worker_id,)).fetchall()
        voided = {r["void_of"] for r in rows if r["kind"] == "void"}
        live = [r for r in rows if r["kind"] in ("work", "payment") and r["seq"] not in voided]
        if not live:
            return None
        target = live[-1]
        if max_age_hours is not None:
            age = now - datetime.fromisoformat(target["recorded_at"])
            if age > timedelta(hours=max_age_hours):
                return None
        void = _insert(c, NewEntry(
            worker_id=worker_id, kind="void", work_date=target["work_date"], amount=0, source=source,
            site_id=target["site_id"], contractor=target["contractor"], transcript=transcript,
            void_of=target["seq"]))
    return dict(target), void


@dataclass
class ChainCheck:
    ok: bool
    entries_checked: int
    broken_at_seq: int | None = None
    reason: str | None = None


def verify_chain(db: Database, worker_id: int, media_reader=None) -> ChainCheck:
    """Recompute every hash for a worker. media_reader(path) -> bytes is optional."""
    prev = GENESIS
    rows = db.list_entries(worker_id)
    for expected_seq, row in enumerate(rows, start=1):
        r = dict(row)
        if r["seq"] != expected_seq:
            return ChainCheck(False, expected_seq - 1, expected_seq, "an entry is missing")
        if r["prev_hash"] != prev:
            return ChainCheck(False, expected_seq - 1, r["seq"], "link to previous entry does not match")
        if entry_hash(r) != r["hash"]:
            return ChainCheck(False, expected_seq - 1, r["seq"], "entry was changed after it was recorded")
        if media_reader and r["media_path"] and r["media_sha256"]:
            try:
                if sha256_bytes(media_reader(r["media_path"])) != r["media_sha256"]:
                    return ChainCheck(False, expected_seq - 1, r["seq"], "attached photo or audio was replaced")
            except FileNotFoundError:
                return ChainCheck(False, expected_seq - 1, r["seq"], "attached photo or audio is missing")
        prev = r["hash"]
    return ChainCheck(True, len(rows))
