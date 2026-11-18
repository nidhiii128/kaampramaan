"""Append-only, hash-chained work ledger.

Every entry stores the hash of the previous entry for the same worker. Changing,
deleting or reordering any past entry breaks the chain, and verify_chain() reports
the first entry where it breaks. Media files are fingerprinted with SHA-256 so a
swapped photo or voice note is also detected.
"""
import hashlib
import json
from dataclasses import dataclass

from .db import Database, now_iso

GENESIS = "0" * 64

HASHED_FIELDS = (
    "worker_id", "seq", "kind", "work_date", "site_id", "contractor", "amount", "paid",
    "source", "transcript", "media_sha256", "latitude", "longitude", "recorded_at", "prev_hash",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_hash(fields: dict) -> str:
    canonical = json.dumps({k: fields.get(k) for k in HASHED_FIELDS}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class NewEntry:
    worker_id: int
    kind: str                 # 'work' or 'payment'
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


def append(db: Database, e: NewEntry) -> dict:
    if e.kind not in ("work", "payment"):
        raise ValueError("kind must be 'work' or 'payment'")
    if e.amount is None or e.amount < 0:
        raise ValueError("amount must be a non-negative number")
    with db.connect() as c:
        # BEGIN IMMEDIATE takes the write lock now, so two messages from the same
        # worker arriving together cannot both read the same last seq.
        c.execute("BEGIN IMMEDIATE")
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
        }
        fields["hash"] = entry_hash(fields)
        cols = ", ".join(fields)
        c.execute(f"INSERT INTO entries ({cols}) VALUES ({', '.join('?' for _ in fields)})",
                  tuple(fields.values()))
    return fields


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
