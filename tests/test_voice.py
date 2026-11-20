"""Voice replies: Gemini TTS -> WAV -> ogg/opus -> WhatsApp, with graceful fallbacks."""
import io
import json
import shutil
import wave
from types import SimpleNamespace

import httpx
import pytest

from app import voice
from app.ai import AI, Understanding
from app.main import create_app
from app.whatsapp import WhatsAppClient
from tests.conftest import TODAY, make_settings
from fastapi.testclient import TestClient

PCM = b"\x01\x00" * 2400            # 0.1 s of 24 kHz 16-bit mono


def test_pcm_becomes_a_valid_wav():
    w = wave.open(io.BytesIO(voice.pcm_to_wav(PCM)))
    assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 2400)


def test_sample_rate_is_read_from_geminis_mime_type():
    assert voice.rate_from_mime("audio/L16;codec=pcm;rate=16000") == 16000
    assert voice.rate_from_mime(None) == 24000


def test_speakable_drops_refs_links_and_the_undo_hint_and_stays_short():
    text = 'Saved: 13 Nov, Rs. 700.\nIs mahine / This month: 1 day.\nRef: a1b2c3d4e5f6. Galat hai? "galat" bhejiye.\nhttps://x.y/d/t.pdf'
    out = voice.speakable(text)
    assert "Ref" not in out and "http" not in out and "Is mahine, This month" in out
    long = voice.speakable("Ek baat. " * 200, limit=100)
    assert len(long) <= 100 and long.endswith(".")


class ScriptedTTS:
    def __init__(self, turns):
        self.turns, self.models = list(turns), []

    def generate_content(self, model, contents, config=None):
        self.models.append(model)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        blob = SimpleNamespace(data=turn, mime_type="audio/L16;codec=pcm;rate=24000")
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=blob)]))])


def ai_with(turns):
    ai = AI(api_key="", tts_models=("tts-new", "tts-old"))
    ai.client = SimpleNamespace(models=ScriptedTTS(turns))
    return ai


def test_speak_returns_wav_and_falls_back_to_the_next_model():
    ai = ai_with([RuntimeError("model retired"), PCM])
    wav = ai.speak("Aapke 700 rupaye baaki hain")
    assert wave.open(io.BytesIO(wav)).getnframes() == 2400
    assert ai.client.models.models == ["tts-new", "tts-old"]


def test_speak_gives_up_quietly_when_every_model_fails_or_there_is_no_client():
    assert ai_with([RuntimeError("x"), RuntimeError("y")]).speak("hello") is None
    assert AI(api_key="").speak("hello") is None and ai_with([PCM]).speak("   ") is None


def test_wav_to_ogg_without_ffmpeg_is_none(monkeypatch):
    monkeypatch.setattr("app.voice.shutil.which", lambda name: None)
    assert voice.wav_to_ogg(voice.pcm_to_wav(PCM)) is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_wav_to_ogg_with_ffmpeg_makes_an_ogg_file():
    assert voice.wav_to_ogg(voice.pcm_to_wav(PCM * 5)).startswith(b"OggS")


def test_send_audio_uploads_the_voice_note_then_sends_it(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.url.path, request.content))
        return httpx.Response(200, json={"id": "MEDIA1"})

    real = httpx.Client
    monkeypatch.setattr("app.whatsapp.httpx.Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    wa = WhatsAppClient(make_settings(tmp_path, wa_token="t", wa_phone_number_id="123"))
    wa.send_audio("919", b"OggSdata")
    assert [p for p, _ in calls] == ["/v21.0/123/media", "/v21.0/123/messages"]
    assert b"OggSdata" in calls[0][1]
    assert json.loads(calls[1][1]) == {"messaging_product": "whatsapp", "to": "919", "type": "audio",
                                       "audio": {"id": "MEDIA1"}}


class SpeakingAI(AI):
    spoken: list

    @property
    def enabled(self):
        return True

    def understand(self, **kw):
        return Understanding(intent="ask_balance")

    def speak(self, text):
        self.spoken.append(text)
        return voice.pcm_to_wav(PCM)


def _webhook(msg_type):
    m = {"id": f"m-{msg_type}", "from": "919", "type": msg_type}
    m["text" if msg_type == "text" else "audio"] = {"body": "how many days?"} if msg_type == "text" else {"id": "A1", "mime_type": "audio/ogg"}
    return {"entry": [{"changes": [{"value": {"messages": [m]}}]}]}


@pytest.mark.parametrize("voice_on, msg_type, expect_audio", [(True, "audio", True), (True, "text", False), (False, "audio", False)])
def test_voice_note_gets_a_voice_reply_only_when_enabled(tmp_path, monkeypatch, voice_on, msg_type, expect_audio):
    sent = []
    monkeypatch.setattr(WhatsAppClient, "download_media", lambda self, mid: (b"audio", "audio/ogg; codecs=opus"))
    monkeypatch.setattr(WhatsAppClient, "send_text", lambda self, to, body: sent.append("text"))
    monkeypatch.setattr(WhatsAppClient, "send_audio", lambda self, to, ogg: sent.append(("audio", ogg)))
    monkeypatch.setattr("app.main.wav_to_ogg", lambda wav: b"OggS-fake")
    ai = SpeakingAI(api_key="")
    ai.spoken = []
    app = create_app(make_settings(tmp_path, voice_replies=voice_on), ai=ai, today_fn=lambda: TODAY)
    app.state.db.create_worker("919", name="Raju", state="active")
    assert TestClient(app).post("/webhook", json=_webhook(msg_type)).status_code == 200
    assert ("audio", b"OggS-fake") in sent if expect_audio else not any(isinstance(s, tuple) for s in sent)
    assert "text" in sent                                        # the text reply is always sent
    assert bool(ai.spoken) == expect_audio
