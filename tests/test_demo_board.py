"""Public demo: the welfare-board story (QR check-in, supervisor confirms, 89-day fast-forward, application)."""
from app.board import overview
from tests.conftest import TODAY
from tests.test_demo import demo_client


def check_in_text(c):
    page = c.get("/demo").text
    start = page.index('data-text="HAZIRI ') + len('data-text="')
    return page[start:page.index('"', start)]


def test_demo_reaches_the_90_day_application(tmp_path):
    c = demo_client(tmp_path)
    text = check_in_text(c)
    assert "<svg" in c.get("/demo").text

    reply = c.post("/demo/send", data={"text": text}).json()[0]["text"]
    assert "Present" in reply
    assert "1 checked in" in c.post("/demo/supervisor", data={"text": "MUSTER"}).json()[0]["text"]
    assert "Confirmed 1 of 1" in c.post("/demo/supervisor", data={"text": "CONFIRM"}).json()[0]["text"]

    assert c.post("/demo/history").json()["added"] == 89
    assert c.post("/demo/history").status_code == 409          # once per sandbox
    d = c.get("/demo/ledger").json()
    assert d["summary"]["confirmed"] == 90 and d["summary"]["ready"] and d["chain"]["ok"]
    assert d["entries"][-1]["confirmed_by"] == ["supervisor"]

    reply = c.post("/demo/send", data={"text": "registration"}).json()[0]
    assert "ready for welfare board" in reply["text"]
    assert c.get(reply["document_url"]).content[:4] == b"%PDF"


def test_demo_workers_stay_out_of_the_board_view(tmp_path):
    c = demo_client(tmp_path)
    c.post("/demo/send", data={"text": check_in_text(c)})
    c.post("/demo/history")
    data = overview(c.app.state.db, TODAY)
    assert data["totals"]["workers"] == 0 and data["totals"]["sites"] == 0


def test_demo_reset_removes_confirmations(tmp_path):
    c = demo_client(tmp_path)
    c.post("/demo/send", data={"text": check_in_text(c)})
    c.post("/demo/supervisor", data={"text": "CONFIRM"})
    assert c.post("/demo/reset").json() == {"reset": True}
    with c.app.state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attestations").fetchone()[0] == 0
