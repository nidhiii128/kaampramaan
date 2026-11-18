"""Gemini tool calling, tested with a scripted fake Gemini client (no network).

Each test scripts what "Gemini" returns on each turn: a list of tool calls, plain
reply text, or an exception. The tools themselves run for real against SQLite.
"""
from types import SimpleNamespace

from google.genai import errors

from app.ai import AI
from app.chat import Bot, Inbound
from app.db import Database
from tests.conftest import TODAY


def call(name, **args):
    return SimpleNamespace(name=name, args=args)


class ScriptedModels:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def generate_content(self, model, contents, config=None):
        self.requests.append(contents)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, list):
            return SimpleNamespace(function_calls=turn, text=None,
                                   candidates=[SimpleNamespace(content="model turn")])
        return SimpleNamespace(function_calls=None, text=turn, parsed=None, candidates=[])


def busy():
    return errors.APIError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})


def make_bot(tmp_path, turns, last_wage=None):
    db = Database(str(tmp_path / "kp.db"))
    site = db.create_site(project="Sector 62", principal_employer="PWD Delhi", employer_type="government",
                          contractor="Ramesh")
    wid = db.create_worker("91", name="Raju", state="active", site_id=site)
    if last_wage:
        db.update_worker(wid, last_wage=last_wage)
    ai = AI(api_key="", model="main", fallback_models=())
    ai.client = SimpleNamespace(models=ScriptedModels(turns))
    bot = Bot(db=db, ai=ai, media_dir=str(tmp_path / "media"), public_base_url="http://t", today_fn=lambda: TODAY)
    return bot, db, wid


def entries(db, wid):
    return [dict(e) for e in db.list_entries(wid)]


def test_agent_logs_work_through_tool_and_replies_in_workers_words(tmp_path):
    bot, db, wid = make_bot(tmp_path, [
        [call("log_work", wage=700, paid=False, contractor="Ramesh", transcript="aaj Ramesh ke saath 700")],
        "Likh liya: aaj Rs. 700, abhi baaki. Is mahine 1 din, baaki Rs. 700.",
    ])
    out = bot.handle(Inbound(phone="91", text="aaj Ramesh ke saath kaam kiya 700 nahi mile"))
    assert out[0].text.startswith("Likh liya")
    (e,) = entries(db, wid)
    assert e["kind"] == "work" and e["amount"] == 700 and e["paid"] == 0
    assert e["work_date"] == TODAY.isoformat() and e["transcript"] == "aaj Ramesh ke saath 700"


def test_one_message_can_call_two_tools(tmp_path):
    bot, db, wid = make_bot(tmp_path, [
        [call("log_work", wage=700, paid=False, transcript="worked 700"),
         call("record_payment", amount=500, transcript="got 500")],
        "Saved Rs. 700 for today and Rs. 500 received. Rs. 200 still due.",
    ])
    bot.handle(Inbound(phone="91", text="worked today 700, Ramesh gave 500"))
    assert [e["kind"] for e in entries(db, wid)] == ["work", "payment"]
    # the tool results went back to Gemini on the second turn
    second = bot.ai.client.models.requests[1]
    responses = [p.function_response for p in second[-1].parts]
    assert responses[1].response == {"saved": True, "amount": 500, "still_due": 200}


def test_reply_with_invented_number_is_replaced_by_fixed_reply(tmp_path):
    bot, db, wid = make_bot(tmp_path, [
        [call("log_work", wage=700, transcript="worked 700")],
        "Saved. You are owed Rs. 9,000.",          # 9000 came from nowhere
    ])
    out = bot.handle(Inbound(phone="91", text="worked today 700"))
    assert "9,000" not in out[0].text and "Due: Rs. 700" in out[0].text


def test_missing_wage_comes_back_as_tool_error(tmp_path):
    bot, db, wid = make_bot(tmp_path, [
        [call("log_work", transcript="aaj kaam kiya")],
        "Aaj ki mazdoori kitni thi?",
    ])
    out = bot.handle(Inbound(phone="91", text="aaj kaam kiya"))
    assert entries(db, wid) == [] and "mazdoori" in out[0].text
    second = bot.ai.client.models.requests[1]
    assert "wage missing" in second[-1].parts[0].function_response.response["error"]


def test_out_of_range_wage_from_model_is_not_saved(tmp_path):
    bot, db, wid = make_bot(tmp_path, [[call("log_work", wage=5_000_000, transcript="x")], "ok"])
    bot.handle(Inbound(phone="91", text="worked"))
    assert entries(db, wid) == []


def test_complaint_tool_attaches_pdf_to_geminis_reply(tmp_path):
    bot, db, wid = make_bot(tmp_path, [
        [call("log_work", wage=700, transcript="w")], "ok",
        [call("make_complaint")], "Aapke Rs. 700 baaki hain. Shikayat bhej di hai.",
    ])
    bot.handle(Inbound(phone="91", text="worked 700"))
    out = bot.handle(Inbound(phone="91", text="Ramesh paisa nahi de raha"))
    assert out[0].text.startswith("Aapke Rs. 700") and out[0].document_url.startswith("http://t/d/")


def test_gemini_down_before_any_tool_falls_back_to_parser(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    bot, db, wid = make_bot(tmp_path, [busy()] * 6)   # agent tries, then understand() tries
    out = bot.handle(Inbound(phone="91", text="Worked with Ramesh today, 700 not paid yet"))
    assert len(entries(db, wid)) == 1 and "Rs. 700" in out[0].text


def test_gemini_down_after_a_tool_ran_does_not_log_twice(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    bot, db, wid = make_bot(tmp_path, [[call("log_work", wage=700, transcript="w")]] + [busy()] * 2)
    out = bot.handle(Inbound(phone="91", text="Worked today 700"))
    assert len(entries(db, wid)) == 1 and "Due: Rs. 700" in out[0].text


def test_unknown_tool_is_ignored(tmp_path):
    bot, db, wid = make_bot(tmp_path, [[call("delete_all_records")], "Send a voice note to log work."])
    out = bot.handle(Inbound(phone="91", text="hi"))
    assert entries(db, wid) == [] and "voice note" in out[0].text
