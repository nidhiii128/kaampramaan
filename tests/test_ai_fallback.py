"""Model fallback and the number guard, tested with a fake Gemini client (no network)."""
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.ai import AI


class FakeModels:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # model name -> exception or text
        self.calls = []

    def generate_content(self, model, contents, config=None):
        self.calls.append(model)
        out = self.behaviour[model]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out, parsed=None)


def make_ai(behaviour):
    ai = AI(api_key="", model="main", fallback_models=("backup", "light"))
    ai.client = SimpleNamespace(models=FakeModels(behaviour))
    return ai


def busy(code):
    return errors.APIError(code, {"error": {"code": code, "message": "busy", "status": "UNAVAILABLE"}})


def test_moves_to_next_model_when_busy():
    ai = make_ai({"main": busy(503), "backup": busy(429), "light": "ok"})
    assert ai._generate("hi").text == "ok"
    assert ai.client.models.calls == ["main", "backup", "light"]


def test_does_not_hide_real_errors():
    ai = make_ai({"main": busy(400), "backup": "ok", "light": "ok"})
    with pytest.raises(errors.APIError):
        ai._generate("hi")


def test_understand_falls_back_to_parser_when_all_models_busy(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ai = make_ai({"main": busy(503), "backup": busy(503), "light": busy(503)})
    u = ai.understand(text="Worked with Ramesh today, 700 not paid yet")
    assert u.intent == "log_work" and u.wage == 700


FACTS = {"worker_name": "Raju", "site": "Sector 62", "contractor": "Ramesh", "principal_employer": "PWD",
         "unpaid_days": 2, "from_date": "2026-11-17", "to_date": "2026-11-18", "due": "1,400"}


def test_complaint_text_with_new_number_is_rejected():
    ai = make_ai({"main": "Raju is owed Rs. 9,999 for 2 days.", "backup": "x", "light": "x"})
    assert "1,400" in ai.complaint_paragraph(FACTS)  # fell back to the template


def test_complaint_text_using_only_known_numbers_is_kept():
    text = "Raju worked 2 days at Sector 62 and is owed Rs. 1,400 by Ramesh."
    ai = make_ai({"main": text, "backup": "x", "light": "x"})
    assert ai.complaint_paragraph(FACTS) == text


def test_failed_model_is_skipped_on_next_message():
    ai = make_ai({"main": busy(504), "backup": "ok", "light": "ok"})
    ai._generate("first")
    ai._generate("second")
    # main timed out once, so the second message goes straight to backup
    assert ai.client.models.calls == ["main", "backup", "backup"]


def test_all_models_cooling_down_still_tries_once():
    ai = make_ai({"main": busy(429), "backup": busy(429), "light": busy(429)})
    with pytest.raises(errors.APIError):
        ai._generate("a")
    with pytest.raises(errors.APIError):
        ai._generate("b")
    assert ai.client.models.calls.count("main") == 2
