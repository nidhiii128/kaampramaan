"""Welfare board view: who is ready to register, who needs confirmation, whose registration is due for renewal,
and which records look wrong. Pure reads over the ledger; nothing here changes data."""
from dataclasses import dataclass
from datetime import date, timedelta

from .db import Database
from .records import WELFARE_DAYS_REQUIRED, Summary, Verification, summarise, verify_days

RENEWAL_DAYS = 365        # most boards renew yearly; set this to your state's period
RENEWAL_WARNING_DAYS = 30
CLOSE_TO_READY = 60       # employer-confirmed days at which a worker is shown as "close"


@dataclass
class WorkerRow:
    w: object                 # sqlite Row from db.list_workers()
    s: Summary
    v: Verification
    renewal_on: date | None = None

    @property
    def registered(self) -> bool:
        return bool(self.w["board_reg_no"])


def worker_rows(db: Database, today: date) -> list[WorkerRow]:
    rows = []
    for w in db.list_workers():
        entries = db.list_entries(w["id"])
        v = verify_days(entries, db.attestations_for(w["id"]), db.corroboration_counts(w["id"]), today=today)
        renewal = None
        if w["board_reg_date"]:
            try:
                renewal = date.fromisoformat(w["board_reg_date"]) + timedelta(days=RENEWAL_DAYS)
            except ValueError:
                renewal = None
        rows.append(WorkerRow(w, summarise(entries, today=today), v, renewal))
    return rows


def overview(db: Database, today: date) -> dict:
    rows = worker_rows(db, today)
    unregistered = [r for r in rows if not r.registered]
    ready = [r for r in unregistered if r.v.ready_to_register]
    # enough days on record, but not enough of them confirmed by the employer side: a supervisor or register is missing
    needs_confirmation = [r for r in unregistered if not r.v.ready_to_register
                          and r.v.total_days_12m >= WELFARE_DAYS_REQUIRED]
    close = [r for r in unregistered if not r.v.ready_to_register and r not in needs_confirmation
             and r.v.employer_days_12m >= CLOSE_TO_READY]
    registered = [r for r in rows if r.registered]
    renewals = [r for r in registered if r.renewal_on and (r.renewal_on - today).days <= RENEWAL_WARNING_DAYS]
    two_sites = db.same_day_two_sites()
    duplicates = db.possible_duplicate_people()
    ready.sort(key=lambda r: -r.v.employer_days_12m)
    close.sort(key=lambda r: -r.v.employer_days_12m)
    renewals.sort(key=lambda r: r.renewal_on)
    return {
        "ready": ready, "needs_confirmation": needs_confirmation, "close": close, "registered": registered,
        "renewals": renewals, "two_sites": two_sites, "duplicates": duplicates,
        "totals": {
            "sites": len(db.list_sites(today.isoformat())),
            "workers": len(rows),
            "confirmed_days": sum(r.v.employer_days_12m for r in rows),
            "all_days": sum(r.v.total_days_12m for r in rows),
            "ready": len(ready),
            "applications": sum(1 for r in unregistered if r.w["application_at"]),
            "registered": len(registered),
            "renewals": len(renewals),
            "flags": len(two_sites) + len(duplicates),
        },
    }
