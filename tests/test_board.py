"""Welfare board flow: site QR check-in, supervisor confirmation, muster register photos, verified 90-day rule,
board view, pre-filled application and renewals."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import attest, ledger, muster
from app.ai import MusterRow, MusterSheet
from app.board import overview
from app.main import create_app
from app.records import LEVEL_COWORKERS, LEVEL_EMPLOYER, LEVEL_SELF, verify_days
from tests.conftest import TODAY, BoardReadingAI, make_settings

SUP = "919811100000"


class MusterReadingAI(BoardReadingAI):
    """Pretends Gemini read a register with two workers, one future day and one impossible day."""
    def read_muster(self, image, mime="image/jpeg", known_names=None):
        self.known = known_names
        return MusterSheet(is_muster_roll=True, month=TODAY.month, year=TODAY.year, rows=[
            MusterRow(name="raju kumar", present_days=[2, 3, 4, 40]),
            MusterRow(name="Sita Devi", present_days=[3, TODAY.day + 1]),
            MusterRow(name="  ", present_days=[5]),
        ], notes="Row 7 smudged")


@pytest.fixture
def app_client(tmp_path):
    app = create_app(make_settings(tmp_path, wa_number="919800000000"), ai=MusterReadingAI(), today_fn=lambda: TODAY)
    c = TestClient(app)
    c.auth = ("admin", "secret")
    return c


def site_with_supervisor(c, sup=SUP):
    r = c.post("/sites", data={"project": "Tower B", "principal_employer": "Skyline Developers", "employer_type": "developer",
                               "contractor": "verma", "supervisor_name": "Mohan", "supervisor_phone": sup[2:]},
               follow_redirects=False)
    assert r.status_code == 303
    site_id = int(r.headers["location"].rsplit("/", 1)[1])
    return c.app.state.db.get_site(site_id)


def say(c, phone, text=None, image=None):
    files = {"file": ("x.jpg", image, "image/jpeg")} if image else None
    r = c.post("/simulator/send", data={"phone": phone, "text": text or ""}, files=files)
    assert r.status_code == 200, r.text
    return r.json()[0]["text"]


# ---------------------------------------------------------------- check-in
def test_new_worker_joins_by_scanning_site_qr(app_client):
    site = site_with_supervisor(app_client)
    assert site["code"] and len(site["code"]) == 6 and site["supervisor_phone"] == SUP
    first = say(app_client, "911", f"HAZIRI {site['code']}")
    assert "Tower B" in first and "name and village" in first
    joined = say(app_client, "911", "Raju Kumar, Bhojpur")
    assert "Present" in joined and "Tower B" in joined and "0 confirmed" in joined
    db = app_client.app.state.db
    w = db.get_worker_by_phone("911")
    assert w["state"] == "active" and w["site_id"] == site["id"]
    [e] = db.list_entries(w["id"])
    assert (e["source"], e["amount"], e["work_date"]) == ("site_qr", 0, TODAY.isoformat())


def test_check_in_once_per_day_and_bad_code(app_client):
    site = site_with_supervisor(app_client)
    say(app_client, "911", f"haziri {site['code'].lower()}")
    say(app_client, "911", "Raju Kumar, Bhojpur")
    assert "already marked present" in say(app_client, "911", f"HAZIRI {site['code']}")
    assert "was not found" in say(app_client, "911", "HAZIRI ZZZZZZ")
    w = app_client.app.state.db.get_worker_by_phone("911")
    assert len(app_client.app.state.db.list_entries(w["id"])) == 1


def test_wage_by_voice_after_check_in_does_not_add_a_day(app_client):
    site = site_with_supervisor(app_client)
    say(app_client, "911", f"HAZIRI {site['code']}")
    say(app_client, "911", "Raju Kumar, Bhojpur")
    say(app_client, "911", "Worked with Verma today, 700 not paid yet")
    assert "Total days worked: 1" in say(app_client, "911", "How many days?")


def test_galat_cancels_check_in(app_client):
    site = site_with_supervisor(app_client)
    say(app_client, "911", f"HAZIRI {site['code']}")
    say(app_client, "911", "Raju Kumar, Bhojpur")
    assert "attendance" in say(app_client, "911", "galat")


# ---------------------------------------------------------------- supervisor
def test_supervisor_lists_and_confirms_on_whatsapp(app_client):
    site = site_with_supervisor(app_client)
    for phone, name in (("911", "Raju Kumar, Bhojpur"), ("912", "Sita Devi, Gaya"), ("913", "Imran Ali, Malda")):
        say(app_client, phone, f"HAZIRI {site['code']}")
        say(app_client, phone, name)
    listing = say(app_client, SUP, "muster")
    assert "3 checked in" in listing and "1. Raju Kumar" in listing and "CONFIRM" in listing
    assert "Numbers 1 to 3" in say(app_client, SUP, "CONFIRM 9")
    assert "Confirmed 2 of 2" in say(app_client, SUP, "confirm 1 3")
    assert "(confirmed)" in say(app_client, SUP, "MUSTER")
    assert "Confirmed 1 of 3" in say(app_client, SUP, "CONFIRM")     # only Sita was left
    db = app_client.app.state.db
    w = db.get_worker_by_phone("912")
    assert list(db.attestations_for(w["id"]).values()) == [{"supervisor"}]
    assert "1 confirmed by your employer" in say(app_client, "912", "How many days?")


def test_supervisor_with_no_check_ins(app_client):
    site_with_supervisor(app_client)
    assert "No check-ins yet" in say(app_client, SUP, "hello")
    assert "Nothing to confirm" in say(app_client, SUP, "CONFIRM")
    assert app_client.app.state.db.get_worker_by_phone(SUP) is None     # a supervisor is not made a worker


def test_officer_confirms_on_website(app_client):
    site = site_with_supervisor(app_client)
    say(app_client, "911", f"HAZIRI {site['code']}")
    say(app_client, "911", "Raju Kumar, Bhojpur")
    page = app_client.get(f"/sites/{site['id']}").text
    assert "Raju Kumar" in page and site["code"] in page
    [row] = app_client.app.state.db.site_day(site["id"], TODAY.isoformat())
    app_client.post(f"/sites/{site['id']}/confirm", data={"day": TODAY.isoformat(), "entry_id": [str(row["id"])]})
    w = app_client.app.state.db.get_worker_by_phone("911")
    assert app_client.app.state.db.attestations_for(w["id"]) == {row["id"]: {"officer"}}


def test_poster_has_qr_with_whatsapp_link(app_client):
    site = site_with_supervisor(app_client)
    page = app_client.get(f"/sites/{site['id']}/poster").text
    assert "<svg" in page and f"HAZIRI {site['code']}" in page and "+919800000000" in page


# ---------------------------------------------------------------- muster register photo
def test_muster_photo_from_supervisor_is_a_draft_until_reviewed(app_client):
    site = site_with_supervisor(app_client)
    reply = say(app_client, SUP, image=b"\xff\xd8 register")
    assert "2 workers" in reply and "officer will review" in reply
    db = app_client.app.state.db
    [imp] = db.list_muster_imports(site["id"])
    assert imp["status"] == "draft"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0     # nothing saved yet
    page = app_client.get(f"/muster/{imp['id']}").text
    assert "Raju Kumar" in page and "Row 7 smudged" in page and "outside the month" in page


def test_muster_import_adds_and_confirms_days(app_client):
    site = site_with_supervisor(app_client)
    say(app_client, "911", f"HAZIRI {site['code']}")          # Raju already checked in today by QR
    say(app_client, "911", "Raju Kumar, Bhojpur")
    say(app_client, SUP, image=b"\xff\xd8 register")
    db = app_client.app.state.db
    [imp] = db.list_muster_imports(site["id"])
    today_day = TODAY.day
    form = {"month": TODAY.month, "year": TODAY.year, "count": 2,
            "name_0": "Raju Kumar", "days_0": f"2-4, {today_day}", "keep_0": "1",
            "name_1": "Sita Devi", "days_1": "3", "keep_1": "",                 # unticked: not imported
            "name_2": "Gopal", "days_2": "5", "keep_2": "1"}                   # row Gemini missed
    r = app_client.post(f"/muster/{imp['id']}", data=form, follow_redirects=False)
    assert r.status_code == 303
    raju = db.get_worker_by_phone("911")
    roles = db.attestations_for(raju["id"])
    assert len(db.list_entries(raju["id"])) == 4                 # 3 from the register + the QR check-in
    assert all(v == {"muster_register"} for v in roles.values()) and len(roles) == 4
    gopal = db.find_worker_at_site(site["id"], "Gopal")
    assert gopal and gopal["phone"] is None
    assert db.find_worker_at_site(site["id"], "Sita Devi") is None
    assert attest.verify_attestations(db, raju["id"]).ok
    assert ledger.verify_chain(db, raju["id"]).ok
    assert app_client.post(f"/muster/{imp['id']}", data=form).status_code == 409   # cannot import twice


def test_clean_sheet_drops_impossible_values():
    sheet = MusterSheet(is_muster_roll=True, month=13, year=1990,
                        rows=[MusterRow(name="a b", present_days=[0, 1, 1, 32, TODAY.day + 1])])
    out = muster.clean_sheet(sheet, TODAY)
    assert (out["month"], out["year"]) == (TODAY.month, TODAY.year)
    assert out["rows"] == [{"name": "A B", "days": [1]}]
    assert "left out" in out["notes"]


def test_parse_days():
    assert muster.parse_days("1, 2,5-7 9-8 x 40", 30) == [1, 2, 5, 6, 7, 8, 9]


# ---------------------------------------------------------------- verification levels
def test_verify_days_levels(db):
    site = db.create_site(project="S")
    ids = [db.create_worker(f"9{i}", name=f"W{i}", state="active", site_id=site) for i in range(4)]
    d1, d2, d3 = "2026-11-10", "2026-11-11", "2026-11-12"
    entries = {}
    for wid in ids:
        for d in (d1, d2) if wid != ids[0] else (d1, d2, d3):
            entries[(wid, d)] = ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=d, amount=0,
                                                                  source="site_qr", site_id=site))
    attest.attest(db, [entries[(ids[0], d1)]], "supervisor", "Mohan")
    w = ids[0]
    v = verify_days(db.list_entries(w), db.attestations_for(w), db.corroboration_counts(w), today=TODAY)
    assert v.by_date == {d1: LEVEL_EMPLOYER, d2: LEVEL_COWORKERS, d3: LEVEL_SELF}
    assert (v.employer_days_12m, v.coworker_days_12m, v.self_days_12m) == (1, 1, 1)
    assert not v.ready_to_register and v.verified_days_to_go == 89


def test_attestation_breaks_if_entry_is_changed(db):
    site = db.create_site(project="S")
    wid = db.create_worker("91", name="A", state="active", site_id=site)
    e = ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date="2026-11-10", amount=0,
                                          source="site_qr", site_id=site))
    attest.attest(db, [e], "supervisor", "Mohan")
    assert attest.verify_attestations(db, wid).ok
    with db.connect() as c:
        c.execute("UPDATE entries SET hash = ? WHERE id = ?", ("f" * 64, e["id"]))
    assert not attest.verify_attestations(db, wid).ok


def test_attest_rejects_bad_input(db):
    with pytest.raises(ValueError):
        attest.attest(db, [], "boss", "x")
    with pytest.raises(ValueError):
        attest.attest(db, [], "supervisor", " ")


# ---------------------------------------------------------------- board view, application, renewal
def _confirmed_days(db, wid, site, n, start=TODAY):
    made, d = [], start
    while len(made) < n:
        made.append(ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=d.isoformat(), amount=0,
                                                      source="site_qr", site_id=site)))
        d -= timedelta(days=1)
    attest.attest(db, made, "supervisor", "Mohan")


def test_board_overview_buckets(db):
    site = db.create_site(project="Tower", supervisor_phone=SUP)
    ready = db.create_worker("91", name="Ready", state="active", site_id=site)
    close = db.create_worker("92", name="Close", state="active", site_id=site)
    reg = db.create_worker("93", name="Registered", state="active", site_id=site)
    _confirmed_days(db, ready, site, 90)
    _confirmed_days(db, close, site, 70)
    _confirmed_days(db, reg, site, 95)
    db.update_worker(reg, board_reg_no="UP/123", board_reg_date=(TODAY - timedelta(days=350)).isoformat())
    db.create_worker(None, name="Close", village=None, state="active")          # same name twice: a flag
    data = overview(db, TODAY)
    assert [r.w["name"] for r in data["ready"]] == ["Ready"]
    assert [r.w["name"] for r in data["close"]] == ["Close"]
    assert [r.w["name"] for r in data["renewals"]] == ["Registered"]
    assert data["totals"]["ready"] == 1 and data["totals"]["registered"] == 1
    assert len(data["duplicates"]) == 1


def test_needs_confirmation_when_days_are_only_self_reported(db):
    site = db.create_site(project="Tower")
    wid = db.create_worker("91", name="Self", state="active", site_id=site)
    for i in range(95):
        ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=(TODAY - timedelta(days=i)).isoformat(),
                                          amount=700, source="whatsapp", site_id=site))
    data = overview(db, TODAY)
    assert [r.w["name"] for r in data["needs_confirmation"]] == ["Self"] and not data["ready"]


def test_same_day_two_sites_flag(db):
    a, b = db.create_site(project="A"), db.create_site(project="B")
    wid = db.create_worker("91", name="X", state="active", site_id=a)
    for s in (a, b):
        ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date="2026-11-10", amount=0,
                                          source="site_qr", site_id=s))
    [flag] = db.same_day_two_sites()
    assert flag["sites"] == 2


def test_application_pdf_and_board_pages(app_client):
    db = app_client.app.state.db
    site = site_with_supervisor(app_client)
    say(app_client, "911", f"HAZIRI {site['code']}")
    say(app_client, "911", "Raju Kumar, Bhojpur")
    w = db.get_worker_by_phone("911")
    past = [ledger.append(db, ledger.NewEntry(worker_id=w["id"], kind="work", source="site_qr", amount=0,
                                              work_date=(TODAY - timedelta(days=i)).isoformat(), site_id=site["id"]))
            for i in range(1, 90)]
    attest.attest(db, past, "supervisor", "Mohan")
    say(app_client, SUP, "CONFIRM")
    assert "ready for welfare board" in say(app_client, "911", "How many days?")

    board = app_client.get("/board").text
    assert "Ready to register (1)" in board and "Raju Kumar" in board

    r = app_client.post(f"/workers/{w['id']}/work-record", follow_redirects=False)
    pdf = app_client.get(r.headers["location"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert db.get_worker(w["id"])["application_at"] == TODAY.isoformat()

    assert app_client.post(f"/workers/{w['id']}/registered",
                           data={"reg_no": "X", "reg_date": (TODAY + timedelta(days=1)).isoformat()}).status_code == 400
    app_client.post(f"/workers/{w['id']}/registered", data={"reg_no": "DL/BOCW/42", "reg_date": "2025-11-20"})
    board = app_client.get("/board").text
    assert "DL/BOCW/42" in board and "Ready to register (0)" in board
    r = app_client.post("/board/reminders", follow_redirects=False)
    assert r.headers["location"] == "/board?sent=1"
    assert "Raju Kumar" in app_client.get(f"/workers/{w['id']}").text


def test_website_complaint_button_returns_pdf(app_client):
    say(app_client, "914", "Hi")
    say(app_client, "914", "Asha Devi, Gaya")
    say(app_client, "914", "skip")
    say(app_client, "914", "Worked with Ramesh today, 700 not paid yet")
    w = app_client.app.state.db.get_worker_by_phone("914")
    r = app_client.post(f"/workers/{w['id']}/complaint", follow_redirects=False)
    assert r.status_code == 303 and app_client.get(r.headers["location"]).content[:4] == b"%PDF"


def test_home_goes_to_board_and_pages_need_login(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path), ai=MusterReadingAI(), today_fn=lambda: TODAY))
    for path in ("/board", "/sites", "/sites/1", "/sites/1/poster", "/muster/1"):
        assert c.get(path).status_code == 401, path
    assert c.get("/", auth=("admin", "secret"), follow_redirects=False).headers["location"] == "/board"


def test_old_database_gets_site_codes(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sites (id INTEGER PRIMARY KEY, project TEXT, principal_employer TEXT, "
                 "employer_type TEXT NOT NULL DEFAULT 'unknown', contractor TEXT, location TEXT, board_photo TEXT, "
                 "created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO sites (project, created_at) VALUES ('Old site', '2026-01-01')")
    conn.commit()
    conn.close()
    from app.db import Database
    db = Database(path)
    site = db.get_site(1)
    assert site["code"] and db.get_site_by_code(site["code"])["project"] == "Old site"
    assert date.fromisoformat("2026-01-01")
