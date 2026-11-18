"""Measure how well work recommendations rank, on labelled test cases.

Usage:
  python -m scripts.eval_recommend            # offline hashing embedder
  python -m scripts.eval_recommend --gemini   # Gemini embeddings (needs GEMINI_API_KEY)

The world below is made up: 12 openings around Delhi NCR, contractors with a payment
history logged by other workers, and test workers who each send one message. Each case
lists the openings a sensible worker should see first. Metrics:
  top-1   share of cases where the first result is a correct one
  top-3   share of cases where a correct one is anywhere in the first three
  MRR     mean of 1 / (rank of the first correct result)
Variants compare meaning-only search against the full ranker, to show what each signal adds.
"""
import argparse
import tempfile
from datetime import date

from app import ledger
from app.config import load_settings
from app.db import Database
from app.recommend import WEIGHTS, CachedEmbedder, GeminiEmbedder, recommend

TODAY = date(2026, 11, 20)

PLACES = {
    "noida62": (28.6270, 77.3720), "noida63": (28.6280, 77.3800), "noida75": (28.5770, 77.3850),
    "gnoida": (28.4740, 77.5040), "indirapuram": (28.6410, 77.3710), "gurgaon49": (28.4110, 77.0470),
    "faridabad": (28.4090, 77.3180), "noida18": (28.5700, 77.3210), "ghaziabad": (28.6690, 77.4530),
    "mayurvihar": (28.6070, 77.2930), "okhla": (28.5310, 77.2710),
}

OPENINGS = {
    "plaster_ramesh": ("Plaster work, interior walls", "Ramesh", "noida62", 750),
    "plaster_suresh": ("Plaster work, new tower", "Suresh", "noida63", 800),
    "shuttering": ("Shuttering carpenter for slab casting", "Verma", "noida75", 850),
    "steel": ("Bar bending and steel fixing", "Verma", "gnoida", 800),
    "painting": ("Painting work, 2 painters needed", "Mohan", "indirapuram", 700),
    "tiles": ("Tile and marble fitting", "Ramesh", "gurgaon49", 900),
    "brick_suresh": ("Brick masonry, boundary wall", "Suresh", "faridabad", 750),
    "helper": ("Helper for loading and unloading material", "Mohan", "noida62", 550),
    "electric": ("Electrician helper, house wiring", "Gupta", "noida18", 650),
    "plumbing": ("Plumbing work, bathroom pipes", "Gupta", "ghaziabad", 700),
    "mason_khan": ("Mason for road kerb and footpath", "Khan", "mayurvihar", 750),
    "welding": ("Welding work, gate fabrication", "Khan", "okhla", 850),
}

# contractor -> (workers who logged days, days each, paid?)
PAY_HISTORY = {"Ramesh": (3, 10, True), "Verma": (2, 12, True), "Mohan": (2, 8, True),
               "Khan": (2, 10, True), "Suresh": (3, 10, False)}

# (message, place, usual wage, past work as (what, contractor, paid) or None, correct openings)
CASES = [
    # same words as the opening
    ("plaster ka kaam chahiye", "noida62", 700, None, {"plaster_ramesh"}),
    ("shuttering ka kaam", "noida75", 800, None, {"shuttering"}),
    ("tile lagane ka kaam", "gurgaon49", 850, None, {"tiles"}),
    ("welding ka kaam", "okhla", 800, None, {"welding"}),
    # Hindi words only, so plain keyword search cannot match them
    ("sariya bandhne ka kaam", "gnoida", 750, None, {"steel"}),
    ("rang rogan ka kaam chahiye", "indirapuram", 650, None, {"painting"}),
    ("deewar ki chinai ka kaam", "mayurvihar", 700, None, {"mason_khan"}),
    ("bijli ka kaam aata hai", "noida18", 600, None, {"electric"}),
    ("nal aur pipe fitting ka kaam", "ghaziabad", 650, None, {"plumbing"}),
    ("chhat ki dhalai ke liye farma", "noida75", 800, None, {"shuttering"}),
    ("maal utaarne chadhane ka kaam", "noida62", 500, None, {"helper"}),
    # vague request: only the worker's own history can help
    ("kaam chahiye", "noida62", 700, ("plaster ka kaam", "Ramesh", True), {"plaster_ramesh"}),
    ("kaam chahiye", "noida75", 800, ("shuttering ka kaam, slab", "Verma", True), {"shuttering"}),
    ("kaam chahiye", "indirapuram", 650, ("painting ka kaam", "Mohan", True), {"painting"}),
]

VARIANTS = {
    "meaning only": {"match": 1.0, "distance": 0, "wage": 0, "reliability": 0, "familiar": 0},
    "meaning + distance + wage": {"match": 0.55, "distance": 0.25, "wage": 0.20, "reliability": 0, "familiar": 0},
    "full ranker": WEIGHTS,
}


def build(db: Database) -> dict[int, str]:
    ids = {}
    for key, (title, contractor, place, wage) in OPENINGS.items():
        lat, lon = PLACES[place]
        oid = db.create_opening(title=title, wage=wage, starts_on="2026-11-18", posted_by="eval",
                                contractor=contractor, location=place, latitude=lat, longitude=lon)
        ids[oid] = key
    n = 0
    for contractor, (workers, days, paid) in PAY_HISTORY.items():
        for _ in range(workers):
            n += 1
            wid = db.create_worker(f"hist{n}", name=f"History {n}", state="active")
            for d in range(days):
                ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=f"2026-10-{d + 1:02d}",
                                                  amount=700, source="eval", contractor=contractor, paid=paid))
    return ids


def add_case_worker(db, i, place, wage, past):
    wid = db.create_worker(f"case{i}", name=f"Case {i}", state="active")
    lat, lon = PLACES[place]
    db.update_worker(wid, last_wage=wage, last_lat=lat, last_lon=lon)
    if past:
        what, contractor, paid = past
        for d in range(5):
            ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=f"2026-11-{d + 1:02d}",
                                              amount=wage, source="eval", contractor=contractor, paid=paid,
                                              transcript=f"{contractor} ke saath {what}"))
    return db.get_worker(wid)


def evaluate(embedder_factory, show_misses=False):
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"{tmp}/eval.db")
        ids = build(db)
        workers = [add_case_worker(db, i, c[1], c[2], c[3]) for i, c in enumerate(CASES)]
        embedder = embedder_factory(db)
        results = {}
        for name, weights in VARIANTS.items():
            hits1 = hits3 = rr = 0.0
            for (msg, _, _, _, good), w in zip(CASES, workers):
                ranked = [ids[r.opening["id"]] for r in
                          recommend(db, w, msg, embedder, TODAY, k=len(OPENINGS), weights=weights,
                                    log_results=False)]
                first = next((i for i, key in enumerate(ranked, 1) if key in good), None)
                hits1 += first == 1
                hits3 += bool(first and first <= 3)
                rr += 1 / first if first else 0
                if show_misses and name == "full ranker" and first != 1:
                    print(f"  miss: {msg!r} -> {ranked[:3]} (wanted {sorted(good)})")
            n = len(CASES)
            results[name] = (hits1 / n, hits3 / n, rr / n)
        return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gemini", action="store_true", help="use Gemini embeddings")
    p.add_argument("--misses", action="store_true", help="print cases the full ranker got wrong")
    args = p.parse_args()

    if args.gemini:
        s = load_settings()
        if not s.gemini_api_key:
            raise SystemExit("GEMINI_API_KEY is empty")
        from google import genai
        client = genai.Client(api_key=s.gemini_api_key)
        factory = lambda db: CachedEmbedder(db, GeminiEmbedder(client, s.gemini_embed_model))
        label = s.gemini_embed_model
    else:
        factory = lambda db: CachedEmbedder(db)
        label = "offline hashing embedder"

    print(f"{len(CASES)} labelled cases, {len(OPENINGS)} openings, embeddings: {label}\n")
    print(f"{'variant':28} {'top-1':>6} {'top-3':>6} {'MRR':>6}")
    for name, (t1, t3, mrr) in evaluate(factory, show_misses=args.misses).items():
        print(f"{name:28} {t1:6.0%} {t3:6.0%} {mrr:6.2f}")


if __name__ == "__main__":
    main()
