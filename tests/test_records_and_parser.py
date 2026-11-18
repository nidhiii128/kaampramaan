from datetime import date, timedelta

from app import ledger
from app.ai import parse_text
from app.records import rupees, summarise

TODAY = date(2026, 11, 13)


def _work(db, wid, day, amount=700, paid=False):
    ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=day, amount=amount,
                                      source="test", paid=paid))


def test_payments_clear_oldest_days_first(db):
    wid = db.create_worker("1")
    for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
        _work(db, wid, d)
    _work(db, wid, "2026-11-04", paid=True)
    ledger.append(db, ledger.NewEntry(worker_id=wid, kind="payment", work_date="2026-11-05",
                                      amount=1000, source="test"))
    s = summarise(db.list_entries(wid), today=TODAY)
    assert s.earned == 2800 and s.paid == 1700 and s.due == 1100
    assert [(u.work_date, u.still_due) for u in s.unpaid_days] == [("2026-11-02", 400), ("2026-11-03", 700)]


def test_welfare_eligibility_counts_distinct_days_in_last_year(db):
    wid = db.create_worker("1")
    for i in range(89):
        _work(db, wid, (TODAY - timedelta(days=i)).isoformat())
    _work(db, wid, TODAY.isoformat())                               # same day twice counts once
    _work(db, wid, (TODAY - timedelta(days=400)).isoformat())       # too old
    s = summarise(db.list_entries(wid), today=TODAY)
    assert s.days_last_12_months == 89 and not s.welfare_eligible and s.days_to_eligibility == 1
    _work(db, wid, (TODAY - timedelta(days=100)).isoformat())
    assert summarise(db.list_entries(wid), today=TODAY).welfare_eligible


def test_rupee_grouping():
    assert rupees(700) == "700"
    assert rupees(12600) == "12,600"
    assert rupees(4813700) == "48,13,700"


def test_parse_work_english():
    u = parse_text("Worked with Ramesh today, 700 not paid yet", today=TODAY)
    assert (u.intent, u.contractor, u.wage, u.paid, u.work_date) == ("log_work", "Ramesh", 700, False, "2026-11-13")


def test_parse_work_hinglish_yesterday():
    u = parse_text("kal Suresh thekedar ke saath kaam kiya 650 rupaye mil gaya", today=TODAY)
    assert u.intent == "log_work" and u.contractor == "Suresh" and u.wage == 650 and u.paid is True
    assert u.work_date == "2026-11-12"


def test_parse_payment_complaint_balance():
    assert parse_text("Ramesh paid me 5,000", today=TODAY).amount == 5000
    assert parse_text("Ramesh has not paid me for two months", today=TODAY).intent == "request_complaint"
    assert parse_text("kitne din hue?", today=TODAY).intent == "ask_balance"
    assert parse_text("Mere bhai ki site pe maut ho gayi", today=TODAY).intent == "report_death"


def test_parse_name():
    u = parse_text("My name is Raju Kumar from Bhojpur", expected="name")
    assert (u.name, u.village) == ("Raju Kumar", "Bhojpur")


def test_today_uses_india_time_not_server_time(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from tests.conftest import make_settings
    s = make_settings(tmp_path)
    assert s.today() == datetime.now(ZoneInfo('Asia/Kolkata')).date()
