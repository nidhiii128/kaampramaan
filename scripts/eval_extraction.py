"""How well does the system understand a worker's message? Measured, not claimed.

Runs 34 hand-written messages in English, Hinglish, Bhojpuri, Bengali and Marathi (romanised, as workers type
them) through the extraction step and compares intent, wage, paid and amount with the expected answer.
Number words ("saat sau" = 700) are included on purpose: they are where a rule-based parser fails.

Usage:
  python -m scripts.eval_extraction --offline          # the built-in rule-based parser (no key needed)
  python -m scripts.eval_extraction                    # Gemini (needs GEMINI_API_KEY in .env)
  python -m scripts.eval_extraction --json out.json    # also save per-case results

The cases were written by the team, not collected from real workers: treat the numbers as a regression check
and a comparison between methods, and replace them with real messages from a pilot as soon as there are some.
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date

from app.ai import AI, parse_text
from app.config import load_settings

TODAY = date(2026, 11, 13)

# (language, message, expected). Only the fields listed in `expected` are checked.
CASES = [
    ("English", "Worked with Ramesh today, 700 not paid yet", {"intent": "log_work", "wage": 700, "paid": False}),
    ("English", "I worked yesterday at the site, got 650 in hand", {"intent": "log_work", "wage": 650, "paid": True}),
    ("English", "Ramesh paid me 5000 today", {"intent": "record_payment", "amount": 5000}),
    ("English", "How many days have I worked this month?", {"intent": "ask_balance"}),
    ("English", "Ramesh has not paid me for two weeks", {"intent": "request_complaint"}),
    ("English", "I need work near Noida, plastering", {"intent": "find_work"}),
    ("English", "Please cancel my last entry, it was wrong", {"intent": "cancel_last"}),
    ("English", "I want my welfare board registration papers", {"intent": "request_registration"}),
    ("Hinglish", "Aaj Ramesh ke saath kaam kiya, 700, abhi nahi mile", {"intent": "log_work", "wage": 700, "paid": False}),
    ("Hinglish", "Aaj site pe kaam kiya, 800 mil gaya", {"intent": "log_work", "wage": 800, "paid": True}),
    ("Hinglish", "Ramesh ne 300 diye", {"intent": "record_payment", "amount": 300}),
    ("Hinglish", "Kitne din hue?", {"intent": "ask_balance"}),
    ("Hinglish", "Ramesh ne paisa nahi diya", {"intent": "request_complaint"}),
    ("Hinglish", "Kaam chahiye Gurgaon mein", {"intent": "find_work"}),
    ("Hinglish", "galat likh diya, wapas karo", {"intent": "cancel_last"}),
    ("Hinglish", "Mujhe labour card banwana hai", {"intent": "request_registration"}),
    ("Hinglish", "Kal kaam kiya saat sau rupaye", {"intent": "log_work", "wage": 700}),
    ("Hinglish", "Ramesh ne paanch hazaar diye", {"intent": "record_payment", "amount": 5000}),
    ("Hinglish", "Mere bhai ki site pe accident mein maut ho gayi", {"intent": "report_death"}),
    ("Bhojpuri", "Aaj Ramesh thekedar ke saath kaam kaile, saat sau rupaiya, abhi na milal",
     {"intent": "log_work", "wage": 700, "paid": False}),
    ("Bhojpuri", "Ramesh paisa na dehlas, hamar mazdoori baaki ba", {"intent": "request_complaint"}),
    ("Bhojpuri", "Ramesh hajaar rupaiya dehle", {"intent": "record_payment", "amount": 1000}),
    ("Bhojpuri", "Hamke kaam chaahi", {"intent": "find_work"}),
    ("Bhojpuri", "Hamar kitna din ho gail?", {"intent": "ask_balance"}),
    ("Bengali", "Aaj Ramesh er shathe kaj korechi, shat sho taka, ekhono dey ni",
     {"intent": "log_work", "wage": 700, "paid": False}),
    ("Bengali", "Ramesh amake taka dey ni", {"intent": "request_complaint"}),
    ("Bengali", "Ramesh aj panch sho taka diyechhe", {"intent": "record_payment", "amount": 500}),
    ("Bengali", "Amar kaj lagbe, Kolkata te", {"intent": "find_work"}),
    ("Bengali", "Koyta din kaj korechi?", {"intent": "ask_balance"}),
    ("Marathi", "Aaj Ramesh sobat kaam kela, saatshe rupaye, ajun dile nahit",
     {"intent": "log_work", "wage": 700, "paid": False}),
    ("Marathi", "Ramesh ne paise dile nahit", {"intent": "request_complaint"}),
    ("Marathi", "Ramesh ne paach hajar dile", {"intent": "record_payment", "amount": 5000}),
    ("Marathi", "Mala kaam pahije", {"intent": "find_work"}),
    ("Marathi", "Kiti divas jhale?", {"intent": "ask_balance"}),
]


def understand(ai, text, offline):
    if offline or not ai.client:
        return parse_text(text, today=TODAY)
    return ai._understand_gemini(text, None, None, "none", TODAY)    # no silent fallback: errors count


def run(offline: bool):
    s = load_settings()
    ai = AI(s.gemini_api_key, s.gemini_model, s.gemini_fallback_models)
    if not offline and not ai.client:
        sys.exit("GEMINI_API_KEY is empty. Add it to .env, or use --offline.")
    results = []
    for lang, text, expected in CASES:
        try:
            got = understand(ai, text, offline)
            actual = {k: getattr(got, k) for k in ("intent", "wage", "paid", "amount")}
            error = None
        except Exception as e:                                   # network, quota, bad JSON
            actual, error = {}, e.__class__.__name__
        checks = {k: actual.get(k) == v for k, v in expected.items()}
        results.append({"language": lang, "text": text, "expected": expected, "actual": actual,
                        "checks": checks, "error": error, "pass": all(checks.values()) and not error})
        if not offline:
            time.sleep(0.4)                                      # stay under free-tier rate limits
    return results, ("offline rule-based parser" if offline or not ai.client else f"Gemini ({ai.model})")


def report(results, method):
    print(f"\nMethod: {method}\n")
    by_lang = defaultdict(lambda: [0, 0])
    fields = defaultdict(lambda: [0, 0])
    for r in results:
        by_lang[r["language"]][1] += 1
        by_lang[r["language"]][0] += bool(r["checks"].get("intent"))
        for k, ok in r["checks"].items():
            fields[k][1] += 1
            fields[k][0] += ok
    pct = lambda a: f"{100 * a[0] / a[1]:.0f}% ({a[0]}/{a[1]})"
    print("Intent correct, by language")
    for lang, a in by_lang.items():
        print(f"  {lang:<9} {pct(a)}")
    print("\nField accuracy (only where the case expects that field)")
    for k in ("intent", "wage", "paid", "amount"):
        if k in fields:
            print(f"  {k:<7} {pct(fields[k])}")
    full = [r for r in results if r["pass"]]
    print(f"\nWhole message right (every checked field): {pct((len(full), len(results)))}")
    failed = [r for r in results if not r["pass"]]
    if failed:
        print("\nMisses")
        for r in failed:
            print(f"  [{r['language']}] {r['text']}\n      expected {r['expected']}  got {r['actual'] or r['error']}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true", help="use the rule-based parser, no API key")
    p.add_argument("--json", help="write per-case results to this file")
    args = p.parse_args()
    results, method = run(args.offline)
    report(results, method)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"method": method, "results": results}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
