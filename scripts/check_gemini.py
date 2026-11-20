"""Check that Gemini works with your key.

Usage:
  python -m scripts.check_gemini                      # text messages only
  python -m scripts.check_gemini --audio note.ogg     # also a voice note
  python -m scripts.check_gemini --image board.jpg    # also a site board photo
  python -m scripts.check_gemini --agent              # also the tool-calling agent
  python -m scripts.check_gemini --tts                # also a Gemini voice reply, saved as tts_test.wav
"""
import argparse
import mimetypes
import os
import sys
import tempfile

from app.ai import AI
from app.chat import Bot, Inbound
from app.config import load_settings
from app.db import Database
from app.recommend import CachedEmbedder, GeminiEmbedder

TEXTS = [
    "Aaj Ramesh thekedar ke saath kaam kiya, 700 rupaye abhi nahi mile",
    "Ramesh ne aaj 5000 diye",
    "Kitne din ho gaye?",
    "Ramesh do mahine se paisa nahi de raha",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio")
    p.add_argument("--image")
    p.add_argument("--agent", action="store_true")
    p.add_argument("--tts", action="store_true")
    args = p.parse_args()

    s = load_settings()
    if not s.gemini_api_key:
        sys.exit("GEMINI_API_KEY is empty. Add it to .env first.")
    ai = AI(s.gemini_api_key, s.gemini_model, s.gemini_fallback_models, s.gemini_tts_models, s.gemini_tts_voice)

    names = [m.name for m in ai.client.models.list()]
    flash = sorted(n for n in names if "flash" in n)
    print(f"Key works. {len(names)} models available. Flash models: {', '.join(flash[:8])}")
    if not any(n.endswith(s.gemini_model) for n in names):
        print(f"WARNING: GEMINI_MODEL={s.gemini_model} is not in the list. Pick one of the above.")

    today = s.today()
    ok = 0
    for t in TEXTS:
        try:
            u = ai._understand_gemini(t, None, None, "none", today)
            print(f"\n> {t}\n  {u.model_dump(exclude_none=True)}")
            ok += 1
        except Exception as e:
            print(f"\n> {t}\n  Gemini busy ({e.__class__.__name__}). The app would use the offline parser here.")
    print(f"\n{ok}/{len(TEXTS)} text messages understood by Gemini.")

    if args.audio:
        mime = mimetypes.guess_type(args.audio)[0] or "audio/ogg"
        with open(args.audio, "rb") as f:
            try:
                u = ai._understand_gemini(None, f.read(), mime, "none", today)
                print(f"\n> voice note {args.audio}\n  {u.model_dump(exclude_none=True)}")
            except Exception as e:
                print(f"\n> voice note {args.audio}\n  Gemini busy ({e.__class__.__name__}). Try again later.")

    if args.image:
        mime = mimetypes.guess_type(args.image)[0] or "image/jpeg"
        with open(args.image, "rb") as f:
            print(f"\n> board photo {args.image}\n  {ai.read_board(f.read(), mime).model_dump()}")

    if args.agent:
        check_agent(ai, today)

    if args.tts:
        wav = ai.speak("Aapke saat sau rupaye baaki hain. Aapka record save ho gaya.")
        if wav:
            with open("tts_test.wav", "wb") as f:
                f.write(wav)
            print(f"\nVoice reply OK with {ai.last_tts_model} ({len(wav)} bytes), saved to tts_test.wav. Play it.")
        else:
            print(f"\nVoice reply FAILED for every model in GEMINI_TTS_MODELS={','.join(ai.tts_models)}. "
                  "Check the model names in the Gemini docs (ai.google.dev/gemini-api/docs/speech-generation) "
                  "and the log above, then set GEMINI_TTS_MODELS in .env.")


AGENT_TEXTS = [
    "Aaj Ramesh thekedar ke saath kaam kiya, 700 rupaye abhi nahi mile",
    "Kal bhi kaam kiya 700, aur Ramesh ne aaj 500 diye",
    "Kitna baaki hai?",
    "Ramesh paisa nahi de raha, shikayat chahiye",
    "Noida mein plaster ka kaam chahiye",
]


def check_agent(ai, today):
    """Run the tool-calling agent on sample messages against a throwaway database."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "kp.db"))
        site = db.create_site(project="Demo site", employer_type="small", contractor="Ramesh")
        db.create_worker("demo", name="Raju", state="active", site_id=site)
        db.create_opening(title="Plaster work, interior walls", wage=750, starts_on=today.isoformat(),
                          posted_by="demo", contractor="Mohan", location="Sector 62, Noida", contact="9800000000")
        db.create_opening(title="Painting work, 2 painters", wage=700, starts_on=today.isoformat(),
                          posted_by="demo", contractor="Mohan", location="Indirapuram", contact="9800000001")
        embedder = CachedEmbedder(db, GeminiEmbedder(ai.client, load_settings().gemini_embed_model))
        bot = Bot(db=db, ai=ai, media_dir=os.path.join(tmp, "media"), public_base_url="http://localhost",
                  today_fn=lambda: today, embedder=embedder)
        real_run = ai.run_agent

        def traced(execute, **kw):
            def logged(name, a):
                result = execute(name, a)
                print(f"  tool {name}({a}) -> {result}")
                return result
            return real_run(logged, **kw)

        ai.run_agent = traced
        for t in AGENT_TEXTS:
            print(f"\n> {t}")
            for r in bot.handle(Inbound(phone="demo", text=t)):
                print(f"  reply: {r.text}" + (f"  [{r.document_name}]" if r.document_name else ""))


if __name__ == "__main__":
    main()
