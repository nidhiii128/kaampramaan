"""Public demo sandbox: no login, per-visitor data, tamper demo, and strict isolation from real data."""
from datetime import date

from fastapi.testclient import TestClient

from app import demo as demo_tools
from app.main import create_app
from app.recommend import contractor_records
from tests.conftest import TODAY, BoardReadingAI, make_settings


def demo_client(tmp_path, **over):
    app = create_app(make_settings(tmp_path, demo_mode=True, **over), ai=BoardReadingAI(), today_fn=lambda: TODAY)
    return TestClient(app)          # no auth: the demo is public


def log_day(c, text="Aaj Ramesh ke saath kaam kiya, 700, abhi nahi mile"):
    return c.post("/demo/send", data={"text": text})


def test_demo_routes_do_not_exist_unless_demo_mode_is_on(client):
    assert client.get("/demo").status_code == 404


def test_demo_page_is_public_and_sets_a_visitor_cookie(tmp_path):
    c = demo_client(tmp_path)
    r = c.get("/demo")
    assert r.status_code == 200 and "Try KaamPramaan" in r.text and 'aria-live="polite"' in r.text
    assert demo_tools.COOKIE in r.cookies
    assert c.get("/dashboard").status_code == 401                 # the staff site is still locked


def test_message_is_logged_and_shown_in_the_ledger_panel(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    replies = log_day(c).json()
    assert "Saved:" in replies[0]["text"] and "Ref: " in replies[0]["text"]
    d = c.get("/demo/ledger").json()
    assert d["chain"]["ok"] and d["summary"]["due"] == 700
    assert [(e["kind"], e["amount"]) for e in d["entries"]] == [("work", 700)]


def test_tamper_is_caught_and_restore_heals_it(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    assert c.post("/demo/tamper").status_code == 409              # nothing to tamper with yet
    log_day(c)
    t = c.post("/demo/tamper").json()
    assert t == {"seq": 1, "was": 700, "now": 70}
    d = c.get("/demo/ledger").json()
    assert not d["chain"]["ok"] and d["chain"]["broken_at_seq"] == 1 and d["tampered"]
    assert c.post("/demo/tamper").status_code == 409              # once is enough
    assert c.post("/demo/restore").json() == {"restored": 700}
    assert c.get("/demo/ledger").json()["chain"]["ok"]


def test_reset_starts_over_with_a_clean_record(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    log_day(c)
    assert c.post("/demo/reset").json() == {"reset": True}
    assert c.get("/demo/ledger").json()["entries"] == []


def test_visitors_cannot_see_each_others_data(tmp_path):
    a, b = demo_client(tmp_path), None
    b = TestClient(a.app)
    a.get("/demo")
    b.get("/demo")
    log_day(a)
    assert b.get("/demo/ledger").json()["entries"] == []


def test_demo_data_never_reaches_real_dashboard_or_contractor_scores(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    log_day(c, "Worked with Verma today, 700 not paid yet")
    c.post("/demo/send", data={"text": "Verma has not paid me"})
    db = c.app.state.db
    assert db.list_workers() == [] and db.all_work_entries() == []
    assert "verma" not in contractor_records(db.all_work_entries(), date(2026, 11, 13))
    assert db.list_wage_cases() == [] and db.impact_stats()["complaints"] == 0


def test_requests_without_a_cookie_are_rejected(tmp_path):
    c = demo_client(tmp_path)
    assert c.post("/demo/send", data={"text": "hi"}).status_code == 400
    assert c.get("/demo/ledger").status_code == 400


def test_oversized_upload_is_refused(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    big = b"0" * (demo_tools.MAX_UPLOAD + 10)
    r = c.post("/demo/send", data={"text": ""}, files={"file": ("v.wav", big, "audio/wav")})
    assert r.status_code == 413


def test_speak_needs_a_gemini_key(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    assert c.post("/demo/speak", data={"text": "hello"}).status_code == 503


def test_stale_demo_workers_are_purged_but_real_ones_are_not(tmp_path):
    c = demo_client(tmp_path)
    c.get("/demo")
    db = c.app.state.db
    real = db.create_worker("919999", name="Real", state="active")
    with db.connect() as conn:
        conn.execute("UPDATE workers SET created_at = '2020-01-01T00:00:00+00:00'")
    assert db.purge_stale_demo(24) == 1
    assert db.get_worker(real) is not None


def test_purge_refuses_real_phone_numbers(tmp_path):
    import pytest
    db = demo_client(tmp_path).app.state.db
    with pytest.raises(ValueError):
        db.purge_demo_worker("919999")


def test_rate_limiter_blocks_the_burst_then_recovers():
    now = [0.0]
    lim = demo_tools.RateLimiter(3, 60, clock=lambda: now[0])
    assert [lim.allow("ip") for _ in range(4)] == [True, True, True, False]
    assert lim.allow("other-ip")
    now[0] = 61
    assert lim.allow("ip")
