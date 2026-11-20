"""Attestations: the site supervisor, the employer's muster register or an officer vouching for a working day.

The worker's ledger is never edited. An attestation is a separate row whose hash covers the hash of the entry it
vouches for, so if the entry is later changed, or the attestation is moved to a different entry, the check fails.
"""
import hashlib
import json
from dataclasses import dataclass

from .db import ATTEST_ROLES, Database, now_iso


def attestation_hash(entry_hash: str, role: str, attester: str, evidence_sha256: str | None, created_at: str) -> str:
    data = {"entry_hash": entry_hash, "role": role, "attester": attester, "evidence_sha256": evidence_sha256,
            "created_at": created_at}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def attest(db: Database, entries, role: str, attester: str, evidence_sha256: str | None = None) -> int:
    """Vouch for each given work entry. Entries this role already vouched for are skipped.
    Returns how many new attestations were saved."""
    if role not in ATTEST_ROLES:
        raise ValueError(f"role must be one of {ATTEST_ROLES}")
    if not attester or not attester.strip():
        raise ValueError("attester is required")
    saved = 0
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for e in entries:
            if e["kind"] != "work":
                raise ValueError("only work entries can be attested")
            created = now_iso()
            h = attestation_hash(e["hash"], role, attester, evidence_sha256, created)
            cur = c.execute(
                "INSERT OR IGNORE INTO attestations (entry_id, worker_id, role, attester, evidence_sha256, "
                "created_at, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (e["id"], e["worker_id"], role, attester.strip(), evidence_sha256, created, h))
            saved += cur.rowcount
    return saved


@dataclass
class AttestationCheck:
    ok: bool
    checked: int
    broken_id: int | None = None
    reason: str | None = None


def verify_attestations(db: Database, worker_id: int) -> AttestationCheck:
    rows = db.list_attestations(worker_id)
    for r in rows:
        expected = attestation_hash(r["entry_hash"], r["role"], r["attester"], r["evidence_sha256"], r["created_at"])
        if expected != r["hash"]:
            return AttestationCheck(False, len(rows), r["id"],
                                    "a confirmation does not match the entry it vouches for")
    return AttestationCheck(True, len(rows))
