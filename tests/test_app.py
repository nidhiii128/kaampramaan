import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import TODAY, BoardReadingAI, make_settings


def say(client, text=None, phone="919900000001", image=None):
    files = {"file": ("board.jpg", image, "image/jpeg")} if image else None
    r = client.post("/simulator/send", data={"phone": phone, "text": text or ""}, files=files)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_worker_conversation(client):
    assert "name and village" in say(client, "Hi")[0]["text"]
    assert "board" in say(client, "Raju Kumar, Bhojpur")[0]["text"]
    site = say(client, image=b"\xff\xd8 fake jpeg")[0]["text"]
    assert "PWD Delhi" in site and "sarkari" in site

    r = say(client, "Worked with Ramesh today, 700 not paid yet")[0]["text"]
    assert "Rs. 700" in r and "Due: Rs. 700" in r
    say(client, "Worked with Ramesh today 700")            # same wage, second entry
    assert "Still due: Rs. 900" in say(client, "Ramesh paid me 500")[0]["text"]
    assert "due Rs. 900" in say(client, "How many days?")[0]["text"]

    complaint = say(client, "Ramesh has not paid me")[0]
    assert complaint["document_url"].startswith("http://testserver/d/")
    pdf = client.get(complaint["document_url"].replace("http://testserver", ""))
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_wage_reused_when_not_said(client):
    for m in ("Hi", "Asha Devi, Gaya", "skip", "Worked today 800"):
        say(client, m, phone="2")
    assert "Rs. 800" in say(client, "Worked today", phone="2")[0]["text"]


def test_dashboard_requires_login(settings):
    c = TestClient(create_app(settings, ai=BoardReadingAI(), today_fn=lambda: TODAY))
    assert c.get("/dashboard").status_code == 401
    assert c.get("/dashboard", auth=("admin", "wrong")).status_code == 401
    assert c.get("/dashboard", auth=("admin", "secret")).status_code == 200


def test_volunteer_logs_group_and_corroborates(client):
    form = {"workers": "Mohan Lal, Gaya\nSuresh Yadav, Ara", "new_site": "Metro depot", "site_id": "",
            "work_date": "2026-11-12", "wage": "650", "contractor": "ramesh"}
    assert "Mohan Lal" in client.post("/volunteer", data=form).text
    page = client.get("/dashboard").text
    assert "Mohan Lal" in page and "Suresh Yadav" in page
    db = client.app.state.db
    mohan = [w for w in db.list_workers() if w["name"] == "Mohan Lal"][0]
    assert list(db.corroboration_counts(mohan["id"]).values()) == [1]


def test_volunteer_rejects_future_date(client):
    form = {"workers": "A, B", "work_date": "2026-12-01", "wage": "650"}
    assert client.post("/volunteer", data=form).status_code == 400


def test_webhook_verification(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                        "hub.challenge": "42"})
    assert ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                         "hub.challenge": "42"})
    assert bad.status_code == 403


def _wa_payload(msg_id, text):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"id": msg_id, "from": "919811111111", "type": "text", "text": {"body": text}}]}}]}]}


def test_webhook_signature_and_dedup(tmp_path):
    s = make_settings(tmp_path, wa_app_secret="app-secret")
    app = create_app(s, ai=BoardReadingAI(), today_fn=lambda: TODAY)
    c = TestClient(app)
    body = json.dumps(_wa_payload("wamid.1", "Hi")).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()

    assert c.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    for _ in range(2):  # Meta may deliver the same message twice
        r = c.post("/webhook", content=body,
                   headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
        assert r.status_code == 200
    worker = app.state.db.get_worker_by_phone("919811111111")
    assert worker["state"] == "awaiting_name"  # processed exactly once


def test_open_dashboard_needs_no_login_and_has_sample_data(tmp_path):
    s = make_settings(tmp_path, admin_password='', open_dashboard=True)
    c = TestClient(create_app(s, ai=BoardReadingAI(), today_fn=lambda: TODAY))
    page = c.get('/dashboard')
    assert page.status_code == 200 and 'Raju Kumar' in page.text


def test_dashboard_still_locked_by_default(settings):
    c = TestClient(create_app(settings, ai=BoardReadingAI(), today_fn=lambda: TODAY))
    assert c.get('/dashboard').status_code == 401
