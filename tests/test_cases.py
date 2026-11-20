"""Wage complaint follow-up: generated -> filed -> resolved, with recovered money flowing into the ledger."""
from app import ledger
from app.chat import Inbound
from app.records import summarise
from tests.conftest import TODAY
from tests.test_agent import make_bot


def _unpaid_worker(db, name="Raju", phone="911"):
    site = db.create_site(project="Sector 62", principal_employer="PWD Delhi", employer_type="government")
    wid = db.create_worker(phone, name=name, state="active", site_id=site)
    for d in ("2026-11-10", "2026-11-11", "2026-11-12"):
        ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=d, amount=700, source="t",
                                          site_id=site, contractor="Ramesh"))
    return wid


def test_asking_for_a_complaint_opens_one_tracked_case_not_duplicates(tmp_path):
    bot, db, wid = make_bot(tmp_path, [])
    bot.ai.client = None
    bot.handle(Inbound(phone="91", text="Worked with Ramesh today, 700 not paid yet"))
    bot.handle(Inbound(phone="91", text="Ramesh has not paid me"))
    bot.handle(Inbound(phone="91", text="Ramesh has not paid me"))
    (case,) = db.list_wage_cases()
    assert case["status"] == "generated" and case["amount_claimed"] == 700 and case["worker_id"] == wid
    assert db.list_cases() == []                       # not mixed into "reports from families"


def test_ngo_records_recovery_and_worker_balance_follows(client):
    db = client.app.state.db
    wid = _unpaid_worker(db)
    case_id = db.open_wage_case(wid, 2100, "3 unpaid days")
    r = client.post(f"/cases/{case_id}", data={"status": "filed", "amount_recovered": 0}, follow_redirects=False)
    assert r.status_code == 303 and db.get_case(case_id)["status"] == "filed"

    r = client.post(f"/cases/{case_id}", data={"status": "resolved", "amount_recovered": 1400}, follow_redirects=False)
    assert r.status_code == 303
    assert summarise(db.list_entries(wid), today=TODAY).due == 700           # 2100 - 1400
    last = dict(db.list_entries(wid)[-1])
    assert last["kind"] == "payment" and last["amount"] == 1400 and last["source"] == "ngo"
    assert ledger.verify_chain(db, wid).ok
    assert db.impact_stats()["recovered"] == 1400 and db.impact_stats()["recovery_rate"] == 67

    page = client.get("/dashboard")
    assert page.status_code == 200 and "Rs. 1,400" in page.text and "wages recovered" in page.text


def test_recovered_amount_cannot_go_down_or_use_a_bad_status(client):
    db = client.app.state.db
    wid = _unpaid_worker(db)
    case_id = db.open_wage_case(wid, 2100)
    client.post(f"/cases/{case_id}", data={"status": "filed", "amount_recovered": 500}, follow_redirects=False)
    assert client.post(f"/cases/{case_id}", data={"status": "filed", "amount_recovered": 100}).status_code == 400
    assert client.post(f"/cases/{case_id}", data={"status": "won", "amount_recovered": 500}).status_code == 400
    assert client.post("/cases/9999", data={"status": "filed", "amount_recovered": 0}).status_code == 404
    assert len([e for e in db.list_entries(wid) if e["kind"] == "payment"]) == 1


def test_database_from_before_case_tracking_gets_new_columns(tmp_path):
    import sqlite3

    from app.db import Database
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cases (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, reporter_phone TEXT, "
                 "worker_id INTEGER, details TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO cases (kind, details, created_at) VALUES ('death_report', 'x', '2026-11-01')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert [c["details"] for c in db.list_cases()] == ["x"]
    assert db.impact_stats()["complaints"] == 0
