"""Work recommendations: retrieval, ranking signals, personalisation, cache and fallback."""
from types import SimpleNamespace

from app import ledger
from app.ai import AI
from app.chat import Bot, Inbound
from app.db import Database
from app.recommend import CachedEmbedder, HashEmbedder, recommend, same_contractor
from tests.conftest import TODAY
from tests.test_agent import ScriptedModels, call

NOIDA = (28.6270, 77.3720)
FAR = (28.4089, 77.3178)          # Faridabad, about 25 km away


def worker(db, phone="91", lat=None, lon=None, wage=700):
    wid = db.create_worker(phone, name="Raju", state="active")
    db.update_worker(wid, last_wage=wage, last_lat=lat, last_lon=lon)
    return wid


def log_days(db, wid, contractor, n, paid, what="plaster ka kaam"):
    for i in range(n):
        ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=f"2026-11-{i + 1:02d}",
                                          amount=700, source="whatsapp", contractor=contractor, paid=paid,
                                          transcript=f"{contractor} ke saath {what}"))


def opening(db, title, contractor="Ramesh", wage=700, at=NOIDA, **kw):
    return db.create_opening(title=title, wage=wage, starts_on="2026-11-14", posted_by="ngo",
                             contractor=contractor, latitude=at[0] if at else None,
                             longitude=at[1] if at else None, **kw)


def top(db, wid, query, k=3):
    w = db.get_worker(wid)
    return recommend(db, w, query, CachedEmbedder(db), TODAY, k=k)


def titles(recs):
    return [r.opening["title"] for r in recs]


def test_contractor_who_owes_wages_ranks_below_one_who_pays(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    for p, c, paid in [("a", "Suresh", False), ("b", "Suresh", False), ("c", "Mohan", True)]:
        log_days(db, worker(db, phone=p), c, 6, paid)
    opening(db, "Plaster work Sector 62", contractor="Suresh Builders")
    opening(db, "Plaster work Sector 62", contractor="Mohan")
    recs = top(db, worker(db, phone="d", lat=NOIDA[0], lon=NOIDA[1]), "plaster")
    assert [r.opening["contractor"] for r in recs] == ["Mohan", "Suresh Builders"]
    assert "owes wages to 2 workers" in recs[1].warning
    assert any("paid all 6 days" in why for why in recs[0].reasons)


def test_nearer_site_ranks_first_when_all_else_is_equal(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    opening(db, "Shuttering work", at=FAR)
    opening(db, "Shuttering work", at=NOIDA)
    recs = top(db, worker(db, lat=NOIDA[0] + 0.01, lon=NOIDA[1]), "shuttering")
    assert recs[0].distance_km == 1 and recs[1].distance_km > 20


def test_vague_request_uses_the_workers_own_history(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    wid = worker(db)
    log_days(db, wid, "Mohan", 5, True, what="plaster ka kaam")
    opening(db, "Painting work, 2 painters", contractor="Verma")
    opening(db, "Plaster work, interior walls", contractor="Verma")
    assert titles(top(db, wid, "kaam chahiye"))[0] == "Plaster work, interior walls"


def test_what_they_ask_for_beats_their_history(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    wid = worker(db)
    log_days(db, wid, "Mohan", 5, True, what="plaster ka kaam")
    opening(db, "Plaster work, interior walls", contractor="Verma")
    opening(db, "Painting work, 2 painters", contractor="Verma")
    assert titles(top(db, wid, "painting ka kaam chahiye"))[0] == "Painting work, 2 painters"


def test_higher_wage_and_past_good_contractor_are_personal(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    wid = worker(db, wage=700)
    log_days(db, wid, "Mohan", 4, True)
    opening(db, "Plaster work", contractor="Verma", wage=700)
    opening(db, "Plaster work", contractor="Mohan", wage=800)
    best = top(db, wid, "plaster")[0]
    assert best.opening["contractor"] == "Mohan"
    assert "more than your usual Rs. 700" in " ".join(best.reasons)
    assert "worked with Mohan before and were paid" in " ".join(best.reasons)


def test_warns_when_contractor_owes_this_worker(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    wid = worker(db)
    log_days(db, wid, "Suresh", 3, False)
    opening(db, "Plaster work", contractor="Suresh")
    (r,) = top(db, wid, "plaster")
    assert r.warning == "Suresh still owes you for 3 days" and r.signals["familiar"] == 0.0


def test_closed_and_ended_openings_are_not_shown(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    db.close_opening(opening(db, "Closed one"))
    opening(db, "Ended one", ends_on="2026-11-01")
    opening(db, "Open one")
    assert titles(top(db, worker(db), "kaam")) == ["Open one"]


def test_shown_results_are_logged_in_order(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    opening(db, "Plaster work")
    opening(db, "Painting work")
    wid = worker(db)
    recs = top(db, wid, "plaster")
    with db.connect() as c:
        rows = c.execute("SELECT opening_id, rank, query FROM recommendations ORDER BY rank").fetchall()
    assert [(r["opening_id"], r["rank"]) for r in rows] == [(x.opening["id"], i) for i, x in enumerate(recs, 1)]
    assert rows[0]["query"] == "plaster"


class CountingEmbedder(HashEmbedder):
    model = "counting"

    def __init__(self, fail=False):
        self.fail, self.texts = fail, []

    def embed(self, texts, task="document"):
        if self.fail:
            raise RuntimeError("quota")
        self.texts += texts
        return super().embed(texts, task)


def test_opening_embeddings_are_cached(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    opening(db, "Plaster work")
    emb = CountingEmbedder()
    cached = CachedEmbedder(db, emb)
    cached.embed_pair(["plaster"], ["Plaster work"])
    cached.embed_pair(["painting"], ["Plaster work"])
    assert emb.texts == ["plaster", "Plaster work", "painting"]   # the opening was embedded once


def test_falls_back_to_offline_embedder_when_gemini_fails(tmp_path):
    db = Database(str(tmp_path / "kp.db"))
    model, q, d = CachedEmbedder(db, CountingEmbedder(fail=True)).embed_pair(["plaster"], ["Plaster work"])
    assert model == "hash-v1" and len(q[0]) == len(d[0]) == 512


def test_same_contractor_ignores_suffixes():
    assert same_contractor("Ramesh Constructions", "ramesh")
    assert same_contractor("Verma Infra Pvt Ltd", "Verma")
    assert not same_contractor("Ramesh", "Suresh") and not same_contractor(None, "Suresh")


# ---------------------------------------------------------------- through the chat
def make_bot(tmp_path, turns=None):
    db = Database(str(tmp_path / "kp.db"))
    ai = AI(api_key="", model="main", fallback_models=())
    if turns is not None:
        ai.client = SimpleNamespace(models=ScriptedModels(turns))
    bot = Bot(db=db, ai=ai, media_dir=str(tmp_path / "m"), public_base_url="http://t", today_fn=lambda: TODAY)
    return bot, db


def test_kaam_chahiye_offline_lists_openings_with_contact(tmp_path):
    bot, db = make_bot(tmp_path)
    worker(db)
    opening(db, "Plaster work", location="Sector 62, Noida", contact="9800000000")
    out = bot.handle(Inbound(phone="91", text="kaam chahiye"))[0].text
    assert "1. Plaster work, Rs. 700/day, Sector 62, Noida. Call 9800000000" in out


def test_agent_calls_find_work_and_uses_its_numbers(tmp_path):
    bot, db = make_bot(tmp_path, [
        [call("find_work", query="plaster Noida")],
        "Noida mein plaster ka kaam hai, Rs. 750 roz. 9800000000 par call karein.",
    ])
    worker(db)
    opening(db, "Plaster work", wage=750, location="Noida", contact="9800000000")
    out = bot.handle(Inbound(phone="91", text="Noida mein plaster ka kaam chahiye"))
    assert out[0].text.startswith("Noida mein plaster")
    sent = bot.ai.client.models.requests[1][-1].parts[0].function_response.response
    assert sent["openings"][0]["wage"] == 750 and sent["openings"][0]["contact"] == "9800000000"


def test_no_openings_says_so(tmp_path):
    bot, db = make_bot(tmp_path)
    worker(db)
    assert "No openings" in bot.handle(Inbound(phone="91", text="kaam chahiye"))[0].text


def test_website_posts_and_closes_openings(client):
    r = client.post("/openings", data={"title": "Plaster work", "wage": "700", "starts_on": "2026-11-14",
                                       "latitude": "28.6", "longitude": "77.3"})
    assert r.status_code == 200 and "Posted: Plaster work" in r.text
    assert client.post("/openings", data={"title": "x", "wage": "700", "starts_on": "2026-11-14",
                                          "latitude": "28.6"}).status_code == 400
    assert client.post("/openings/1/close", follow_redirects=False).status_code == 303
    assert "closed" in client.get("/openings").text
