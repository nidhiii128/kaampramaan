"""Turns a worker's ledger into totals: days worked, wages due, unpaid days, welfare eligibility."""
from dataclasses import dataclass, field
from datetime import date, timedelta

WELFARE_DAYS_REQUIRED = 90  # long-standing welfare board rule: 90 days in the preceding 12 months
# The BOCW Act 1996 this rule came from was folded into the new Labour Codes (in force 21 Nov 2025), so
# documents say "confirm with your state board" instead of citing a section that may no longer apply.
WELFARE_RULE_NOTE = ("90 days in the preceding 12 months (welfare board rule; confirm the current "
                     "requirement with your state building workers' welfare board)")


@dataclass
class UnpaidDay:
    entry_id: int
    work_date: str
    contractor: str | None
    wage: int
    still_due: int
    hash: str


@dataclass
class Summary:
    days_worked: int
    days_last_12_months: int
    days_this_month: int
    earned: int
    paid: int
    due: int
    unpaid_days: list[UnpaidDay] = field(default_factory=list)
    cancelled_entries: int = 0      # entries the worker cancelled; they are excluded from every total

    @property
    def welfare_eligible(self) -> bool:
        return self.days_last_12_months >= WELFARE_DAYS_REQUIRED

    @property
    def days_to_eligibility(self) -> int:
        return max(0, WELFARE_DAYS_REQUIRED - self.days_last_12_months)


def live_entries(entries) -> list[dict]:
    """Work and payment entries that were not cancelled by a later 'void' entry, in ledger order."""
    rows = [dict(e) for e in entries]
    voided = {(r.get("worker_id"), r.get("void_of")) for r in rows if r["kind"] == "void"}
    return [r for r in rows if r["kind"] in ("work", "payment") and (r.get("worker_id"), r["seq"]) not in voided]


def summarise(entries, today: date | None = None) -> Summary:
    """Payments are applied to the oldest unpaid days first, so the list of
    unpaid days is exactly what a complaint should list."""
    today = today or date.today()
    all_rows = [dict(e) for e in entries]
    live = live_entries(all_rows)
    work = [e for e in live if e["kind"] == "work"]
    payments = [e for e in live if e["kind"] == "payment"]
    work.sort(key=lambda e: (e["work_date"], e["seq"]))

    dates = {e["work_date"] for e in work}
    year_ago = (today - timedelta(days=365)).isoformat()
    month_prefix = today.isoformat()[:7]

    earned = sum(e["amount"] for e in work)
    paid_on_spot = sum(e["amount"] for e in work if e["paid"])
    paid_later = sum(p["amount"] for p in payments)

    # Outstanding per day, oldest first, then apply later payments FIFO.
    outstanding = [[e, 0 if e["paid"] else e["amount"]] for e in work]
    pool = paid_later
    for item in outstanding:
        if pool <= 0:
            break
        take = min(pool, item[1])
        item[1] -= take
        pool -= take

    unpaid = [
        UnpaidDay(e["id"], e["work_date"], e["contractor"], e["amount"], left, e["hash"])
        for e, left in outstanding if left > 0
    ]
    return Summary(
        days_worked=len(dates),
        days_last_12_months=len({d for d in dates if d > year_ago and d <= today.isoformat()}),
        days_this_month=len({d for d in dates if d.startswith(month_prefix)}),
        earned=earned,
        paid=paid_on_spot + paid_later,
        due=sum(u.still_due for u in unpaid),
        unpaid_days=unpaid,
        cancelled_entries=sum(1 for e in all_rows if e["kind"] == "void"),
    )


# How well each working day is backed, strongest first. A welfare board can decide which levels it accepts;
# the default "ready to register" rule counts only days vouched for by the employer side.
LEVEL_EMPLOYER = "employer"      # site supervisor confirmed it, or it is on the employer's own muster register
LEVEL_COWORKERS = "co-workers"   # COWORKER_MIN or more other workers logged the same site on the same date
LEVEL_SELF = "self"              # only the worker's own report
LEVELS = (LEVEL_EMPLOYER, LEVEL_COWORKERS, LEVEL_SELF)
COWORKER_MIN = 2
EMPLOYER_ROLES = {"supervisor", "muster_register", "officer"}
LEVEL_TEXT = {
    LEVEL_EMPLOYER: "Confirmed by employer side",
    LEVEL_COWORKERS: f"Backed by {COWORKER_MIN}+ co-workers",
    LEVEL_SELF: "Worker's own report",
}


@dataclass
class Verification:
    by_date: dict[str, str]                      # ISO date -> level, for every working day on record
    verifiers: dict[str, set[str]]               # ISO date -> roles that vouched for that day
    employer_days_12m: int
    coworker_days_12m: int
    self_days_12m: int

    @property
    def ready_to_register(self) -> bool:
        return self.employer_days_12m >= WELFARE_DAYS_REQUIRED

    @property
    def verified_days_to_go(self) -> int:
        return max(0, WELFARE_DAYS_REQUIRED - self.employer_days_12m)

    @property
    def total_days_12m(self) -> int:
        return self.employer_days_12m + self.coworker_days_12m + self.self_days_12m


def verify_days(entries, attestations: dict[int, set[str]], corroboration: dict[int, int],
                today: date | None = None) -> Verification:
    """Give each working day the strongest level any of that day's live entries has.

    attestations: entry id -> roles (from db.attestations_for), corroboration: entry id -> other workers
    (from db.corroboration_counts). Cancelled entries are ignored, the same as in every total."""
    today = today or date.today()
    rank = {lvl: i for i, lvl in enumerate(LEVELS)}
    by_date: dict[str, str] = {}
    verifiers: dict[str, set[str]] = {}
    for e in live_entries(entries):
        if e["kind"] != "work":
            continue
        roles = attestations.get(e["id"], set())
        if roles & EMPLOYER_ROLES:
            level = LEVEL_EMPLOYER
        elif corroboration.get(e["id"], 0) >= COWORKER_MIN:
            level = LEVEL_COWORKERS
        else:
            level = LEVEL_SELF
        d = e["work_date"]
        verifiers.setdefault(d, set()).update(roles)
        if d not in by_date or rank[level] < rank[by_date[d]]:
            by_date[d] = level
    year_ago, now = (today - timedelta(days=365)).isoformat(), today.isoformat()
    recent = [lvl for d, lvl in by_date.items() if year_ago < d <= now]
    return Verification(by_date, verifiers, recent.count(LEVEL_EMPLOYER), recent.count(LEVEL_COWORKERS),
                        recent.count(LEVEL_SELF))


def rupees(n: int) -> str:
    """Indian digit grouping: 1234567 -> 12,34,567"""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])
