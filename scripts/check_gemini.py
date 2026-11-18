"""Check that Gemini works with your key.

Usage:
  python -m scripts.check_gemini                      # text messages only
  python -m scripts.check_gemini --audio note.ogg     # also a voice note
  python -m scripts.check_gemini --image board.jpg    # also a site board photo
"""
import argparse
import mimetypes
import sys
from datetime import date

from app.ai import AI
from app.config import load_settings

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
    args = p.parse_args()

    s = load_settings()
    if not s.gemini_api_key:
        sys.exit("GEMINI_API_KEY is empty. Add it to .env first.")
    ai = AI(s.gemini_api_key, s.gemini_model, s.gemini_fallback_models)

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


if __name__ == "__main__":
    main()
