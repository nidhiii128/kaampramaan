"""Cancelling a mistaken entry: nothing is deleted, the chain stays valid, totals ignore it."""
import sqlite3
from datetime import datetime, timedelta, timezone

from app import ledger
from app.chat import Bot, Inbound
from app.db import Database
from app.records import live_entries, summarise
from tests.conftest import TODAY
from tests.test_agent import call, make_bot
from tests.test_ledger import _add


def test_void_keeps_chain_valid_and_totals_ignore_the_cancelled_entry(db):
    wid = db.create_worker("911")
    _add(db, wid, "2026-11-01", amount=700)
    wrong = _add(db, wid, "2026-11-02", amount=7000)          # misheard wage
    cancelled, void = ledger.void_last(db, wid, source="test")
    assert cancelled["seq"] == wrong["seq"] and void["kind"] == "void" and void["void_of"] == wrong["seq"]
    assert void["prev_hash"] == wrong["hash"]                  # appended, not edited
    assert ledger.verify_chain(db, wid).ok
    s = summarise(db.list_entries(wid), today=TODAY)
    assert (s.days_worked, s.earned, s.due, s.cancelled_entries) == (1, 700, 700, 1)
    assert [e["seq"] for e in live_entries(db.list_entries(wid))] == [1]


def test_tampering_with_a_void_entry_is_detected(db):
    wid = db.create_worker("911")
    _add(db, wid, "2026-11-01")
    _add(db, wid, "2026-11-02")
    ledger.void_last(db, wid, source="test")
    with db.connect() as c:   # someone points the void at a different entry
        c.execute("UPDATE entries SET void_of = 1 WHERE worker_id = ? AND kind = 'void'", (wid,))
    check = ledger.verify_chain(db, wid)
    assert not check.ok and check.broken_at_seq == 3


def test_repeated_void_walks_back_one_entry_at_a_time_and_stops_when_empty(db):
    wid = db.create_worker("911")
    _add(db, wid, "2026-11-01")
    _add(db, wid, "2026-11-02")
    assert ledger.void_last(db, wid, source="t")[0]["seq"] == 2
    assert ledger.void_last(db, wid, source="t")[0]["seq"] == 1
    assert ledger.void_last(db, wid, source="t") is None


def test_old_entries_cannot_be_cancelled(db):
    wid = db.create_worker("911")
    _add(db, wid, "2026-11-01")
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert ledger.void_last(db, wid, source="t", now=later) is None
    assert summarise(db.list_entries(wid), today=TODAY).days_worked == 1


def test_void_cannot_be_appended_through_the_normal_path(db):
    import pytest
    wid = db.create_worker("911")
    with pytest.raises(ValueError):
        ledger.append(db, ledger.NewEntry(worker_id=wid, kind="void", work_date="2026-11-01", amount=0,
                                          source="t", void_of=1))


def test_database_created_before_void_entries_is_migrated_and_still_verifies(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE workers (id INTEGER PRIMARY KEY, phone TEXT UNIQUE, name TEXT, village TEXT, site_id INTEGER,
            state TEXT NOT NULL DEFAULT 'new', last_wage INTEGER, last_lat REAL, last_lon REAL, created_at TEXT NOT NULL);
        CREATE TABLE entries (id INTEGER PRIMARY KEY, worker_id INTEGER NOT NULL, seq INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('work', 'payment')), work_date TEXT NOT NULL, site_id INTEGER,
            contractor TEXT, amount INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
            transcript TEXT, media_path TEXT, media_sha256 TEXT, latitude REAL, longitude REAL,
            recorded_at TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, UNIQUE (worker_id, seq));
        CREATE INDEX idx_entries_site_date ON entries (site_id, work_date);
        INSERT INTO workers (id, phone, created_at) VALUES (1, '911', '2026-11-01');
    """)
    f = dict(worker_id=1, seq=1, kind="work", work_date="2026-11-01", site_id=None, contractor="Ramesh",
             amount=700, paid=0, source="test", transcript="old", media_sha256=None, latitude=None,
             longitude=None, recorded_at="2026-11-01T10:00:00+00:00", prev_hash=ledger.GENESIS)
    f["hash"] = ledger.entry_hash(f)
    conn.execute("INSERT INTO entries (worker_id, seq, kind, work_date, site_id, contractor, amount, paid, source,"
                 " transcript, media_sha256, latitude, longitude, recorded_at, prev_hash, hash)"
                 " VALUES (:worker_id,:seq,:kind,:work_date,:site_id,:contractor,:amount,:paid,:source,"
                 ":transcript,:media_sha256,:latitude,:longitude,:recorded_at,:prev_hash,:hash)", f)
    conn.commit()
    conn.close()

    db = Database(path)                                        # migrates in place
    assert ledger.verify_chain(db, 1).ok
    _add(db, 1, "2026-11-02")
    assert ledger.void_last(db, 1, source="t", max_age_hours=None)[0]["seq"] == 2
    assert ledger.verify_chain(db, 1).ok


def test_corroboration_ignores_cancelled_entries_of_other_workers(db):
    site = db.create_site(project="S")
    a, b = db.create_worker("1"), db.create_worker("2")
    _add(db, a, "2026-11-01", site_id=site)
    _add(db, b, "2026-11-01", site_id=site)
    assert list(db.corroboration_counts(a).values()) == [1]
    ledger.void_last(db, b, source="t")
    assert list(db.corroboration_counts(a).values()) == [0]


# ---------------------------------------------------------------- through the bot
def _say(bot, text):
    return bot.handle(Inbound(phone="91", text=text))[0].text


def test_worker_sends_galat_and_last_entry_is_cancelled(tmp_path):
    bot, db, wid = make_bot(tmp_path, [])
    bot.ai.client = None                                       # offline parser path
    first = _say(bot, "Worked with Ramesh today, 700 not paid yet")
    assert "Ref: " in first and "galat" in first.lower()
    out = _say(bot, "galat")
    assert out.startswith("Cancelled: 13 Nov work, Rs. 700") and "Due: Rs. 0" in out
    assert db.get_worker(wid)["last_wage"] is None             # a wrong wage is not reused as the default
    assert [e["kind"] for e in db.list_entries(wid)] == ["work", "void"]
    _say(bot, "Worked with Ramesh today, 600 not paid yet")
    assert "due Rs. 600" in _say(bot, "How many days?")


def test_galat_with_nothing_to_cancel_is_a_safe_noop(tmp_path):
    bot, db, wid = make_bot(tmp_path, [])
    bot.ai.client = None
    assert "Cancel nahi ho saka" in _say(bot, "galat")
    assert db.list_entries(wid) == []


def test_agent_can_cancel_and_relog_in_one_message(tmp_path):
    bot, db, wid = make_bot(tmp_path, [
        [call("log_work", wage=7000, transcript="worked 7000")], "ok",
        [call("void_last_entry", transcript="galat"), call("log_work", wage=700, transcript="700 tha")],
        "Theek hai, sahi kar diya. Rs. 700 likha.",
    ])
    _say(bot, "worked today 7000")
    out = _say(bot, "galat, 700 tha")
    assert out.startswith("Theek hai") and out.count("Ref: ") == 2
    kinds = [(e["kind"], e["amount"]) for e in db.list_entries(wid)]
    assert kinds == [("work", 7000), ("void", 0), ("work", 700)]
    s = summarise(db.list_entries(wid), today=TODAY)
    assert (s.earned, s.due, s.days_worked) == (700, 700, 1)


def test_complaint_pdf_excludes_cancelled_days_and_says_so(tmp_path):
    from app import pdfs
    bot, db, wid = make_bot(tmp_path, [])
    bot.ai.client = None
    _say(bot, "Worked with Ramesh today, 7000 not paid yet")
    _say(bot, "galat")
    _say(bot, "Worked with Ramesh today, 700 not paid yet")
    worker = db.get_worker(wid)
    entries = db.list_entries(wid)
    s = summarise(entries, today=TODAY)
    out = tmp_path / "c.pdf"
    pdfs.complaint_pdf(str(out), worker, db.get_site(worker["site_id"]), s, ledger.verify_chain(db, wid), {},
                       "paragraph", today=TODAY)
    assert out.stat().st_size > 0 and s.cancelled_entries == 1 and s.due == 700


def test_website_shows_cancelled_entries(client):
    db = client.app.state.db
    wid = db.create_worker("919", name="Raju", state="active")
    ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date="2026-11-12", amount=7000, source="t"))
    r = client.post("/simulator/send", data={"phone": "919", "text": "galat"})
    assert r.status_code == 200 and r.json()[0]["text"].startswith("Cancelled")
    page = client.get(f"/workers/{wid}")
    assert page.status_code == 200 and "Cancelled #1" in page.text and "All 2 entries are intact" in page.text
