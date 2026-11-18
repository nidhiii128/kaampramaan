"""Turns a worker's ledger into totals: days worked, wages due, unpaid days, welfare eligibility."""
from dataclasses import dataclass, field
from datetime import date, timedelta

WELFARE_DAYS_REQUIRED = 90  # BOCW Act 1996, Section 12: 90 days in the preceding 12 months


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

    @property
    def welfare_eligible(self) -> bool:
        return self.days_last_12_months >= WELFARE_DAYS_REQUIRED

    @property
    def days_to_eligibility(self) -> int:
        return max(0, WELFARE_DAYS_REQUIRED - self.days_last_12_months)


def summarise(entries, today: date | None = None) -> Summary:
    """Payments are applied to the oldest unpaid days first, so the list of
    unpaid days is exactly what a complaint should list."""
    today = today or date.today()
    work = [dict(e) for e in entries if e["kind"] == "work"]
    payments = [dict(e) for e in entries if e["kind"] == "payment"]
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
    )


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
