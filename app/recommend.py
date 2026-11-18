"""Matching workers to work openings: retrieval, then ranking, then personalisation.

1. Retrieval. The worker's request ("plaster ka kaam chahiye Noida mein") and every open
   opening are embedded. Openings are ordered by cosine similarity and the top
   RETRIEVE_K go on to ranking. When the request is vague ("kaam chahiye"), the worker's
   own past work, taken from their ledger, is used as the query instead.
2. Ranking. Each candidate gets a score from five signals, each between 0 and 1:
     match        how close the opening is to what the worker asked for
     distance     how far the site is from the worker's last shared location
     wage         the opening's wage compared with what this worker usually earns
     reliability  how much of the wages logged against this contractor, by all workers,
                  were actually paid (from the ledger, FIFO matched)
     familiar     whether this worker was paid by this contractor before, or is owed money
3. Every shown result is logged in the recommendations table, so ranking changes can be
   measured on real data later.

Embeddings come from Gemini when a key is set. Without one, or when Gemini fails, a small
hashing embedder is used so the feature still works offline. Both sides of a comparison
always use the same embedder.
"""
import hashlib
import json
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .records import summarise

log = logging.getLogger(__name__)

WEIGHTS = {"match": 0.40, "distance": 0.20, "wage": 0.15, "reliability": 0.15, "familiar": 0.10}
RETRIEVE_K = 20
PROFILE_ENTRIES = 30

GENERIC_WORDS = {
    "kaam", "kam", "work", "job", "jobs", "naukri", "chahiye", "chaiye", "chahie", "need", "want", "koi",
    "mujhe", "muje", "mere", "mera", "liye", "lie", "mil", "milega", "milegi", "hai", "kya", "any", "find",
    "dhundo", "please", "naya", "nayi", "new", "some", "a", "i", "me", "for", "the", "ka", "ki", "ke", "se",
    "do", "de", "dijiye", "batao", "bataiye", "there", "is", "get", "kuch", "aaj", "kal",
}
CONTRACTOR_SUFFIXES = {"constructions", "construction", "builders", "builder", "infra", "contractor",
                       "contractors", "thekedar", "thekedaar", "pvt", "ltd", "co", "and", "sons", "ji", "seth"}


# ---------------------------------------------------------------- embedders
def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # vectors are stored unit length


class HashEmbedder:
    """Offline fallback: signed feature hashing of words and character trigrams.
    Catches shared words and spellings ("plaster", "plastering"), not meaning."""
    model = "hash-v1"
    dim = 512

    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            feats = [f"w:{word}"] + [f"c:{word[i:i + 3]}" for i in range(max(1, len(word) - 2))]
            for f in feats:
                h = int.from_bytes(hashlib.sha1(f.encode()).digest()[:8], "big")
                v[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        return _unit(v)


class GeminiEmbedder:
    """Gemini embeddings, with separate task types for queries and documents."""

    def __init__(self, client, model: str):
        self.client, self.model = client, model

    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        from google.genai import types
        resp = self.client.models.embed_content(
            model=self.model, contents=texts,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT"),
        )
        return [_unit(list(e.values)) for e in resp.embeddings]


class CachedEmbedder:
    """Wraps an embedder with the SQLite cache, and falls back to HashEmbedder for the
    whole request if the main embedder fails, so queries and openings stay comparable."""

    def __init__(self, db, primary=None):
        self.db = db
        self.primary = primary
        self.backup = HashEmbedder()

    def embed_pair(self, queries: list[str], docs: list[str]):
        """Returns (model, query vectors, doc vectors), all from the same embedder."""
        for emb in ([self.primary] if self.primary else []) + [self.backup]:
            try:
                return emb.model, emb.embed(queries, task="query"), self._docs(emb, docs)
            except Exception:
                log.exception("Embedder %s failed, trying the next one", emb.model)
        raise RuntimeError("no embedder available")

    def _docs(self, emb, docs: list[str]) -> list[list[float]]:
        hashes = [hashlib.sha256(d.encode("utf-8")).hexdigest() for d in docs]
        cached = self.db.get_embeddings(emb.model, list(set(hashes)))
        missing = sorted({h: d for h, d in zip(hashes, docs) if h not in cached}.items())
        if missing:
            vectors = emb.embed([d for _, d in missing], task="document")
            new = {h: json.dumps([round(x, 6) for x in v]) for (h, _), v in zip(missing, vectors)}
            self.db.put_embeddings(emb.model, new)
            cached.update(new)
        return [json.loads(cached[h]) for h in hashes]


# ---------------------------------------------------------------- signals
def norm_contractor(name: str | None) -> str:
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in CONTRACTOR_SUFFIXES]
    return " ".join(words)


def same_contractor(a: str | None, b: str | None) -> bool:
    a, b = norm_contractor(a), norm_contractor(b)
    if not a or not b:
        return False
    return a == b or a.split()[0] == b.split()[0]


@dataclass
class PayRecord:
    days: int = 0
    unpaid_days: int = 0
    workers_owed: set = field(default_factory=set)

    @property
    def paid_days(self) -> int:
        return self.days - self.unpaid_days

    @property
    def score(self) -> float:
        # Laplace smoothing: a new contractor with 1 paid day is not "100% reliable"
        return (self.paid_days + 1) / (self.days + 2)


def contractor_records(entries, today: date) -> dict[str, PayRecord]:
    """Per contractor, across all workers: days logged and days still unpaid (FIFO matched)."""
    by_worker = defaultdict(list)
    for e in entries:
        by_worker[e["worker_id"]].append(e)
    records: dict[str, PayRecord] = defaultdict(PayRecord)
    for wid, rows in by_worker.items():
        for e in rows:
            if e["kind"] == "work" and norm_contractor(e["contractor"]):
                records[norm_contractor(e["contractor"])].days += 1
        for u in summarise(rows, today=today).unpaid_days:
            key = norm_contractor(u.contractor)
            if key:
                records[key].unpaid_days += 1
                records[key].workers_owed.add(wid)
    return records


def find_record(records: dict[str, PayRecord], contractor: str | None) -> PayRecord | None:
    for key, rec in records.items():
        if same_contractor(key, contractor):
            return rec
    return None


def km_between(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def is_vague(query: str | None) -> bool:
    words = re.findall(r"[a-z]+", (query or "").lower())
    return not [w for w in words if w not in GENERIC_WORDS]


def opening_text(o) -> str:
    parts = [o["title"], o["details"], o["location"], f"contractor {o['contractor']}" if o["contractor"] else None]
    return ". ".join(p for p in parts if p)


def profile_text(work_entries) -> str | None:
    """What this worker has done before, from their own ledger (most recent first)."""
    recent = [e for e in work_entries if e["kind"] == "work"][-PROFILE_ENTRIES:][::-1]
    said = [e["transcript"] for e in recent if e["transcript"] and not e["transcript"].startswith("Logged by")]
    return ". ".join(dict.fromkeys(said)) or None


def usual_wage(worker, work_entries) -> int | None:
    if worker["last_wage"]:
        return worker["last_wage"]
    wages = sorted(e["amount"] for e in work_entries if e["kind"] == "work")
    return wages[len(wages) // 2] if wages else None


# ---------------------------------------------------------------- recommend
@dataclass
class Recommendation:
    opening: dict
    score: float
    signals: dict
    reasons: list[str]
    warning: str | None = None
    distance_km: int | None = None


def recommend(db, worker, query: str | None, embedder: CachedEmbedder, today: date, k: int = 3,
              weights: dict | None = None, log_results: bool = True) -> list[Recommendation]:
    weights = weights or WEIGHTS
    openings = [dict(o) for o in db.list_open_openings(today.isoformat())]
    if not openings:
        return []
    mine = [dict(e) for e in db.list_entries(worker["id"])]
    profile = profile_text(mine)
    vague = is_vague(query)

    # 1. retrieval
    queries = [q for q in ([None if vague else query] + [profile]) if q]
    if queries:
        model, qvecs, dvecs = embedder.embed_pair(queries, [opening_text(o) for o in openings])
        qsim = [[max(0.0, cosine(q, d)) for d in dvecs] for q in qvecs]
        if vague:                              # only the profile
            match = qsim[0]
        elif profile:                          # what they asked for, nudged by what they usually do
            match = [0.75 * a + 0.25 * b for a, b in zip(qsim[0], qsim[1])]
        else:
            match = qsim[0]
    else:                                      # vague request and no history: nothing to match on
        match = [0.5] * len(openings)
    order = sorted(range(len(openings)), key=lambda i: match[i], reverse=True)[:RETRIEVE_K]

    # 2. ranking
    records = contractor_records(db.all_work_entries(), today)
    my_records = contractor_records(mine, today)
    wage_usual = usual_wage(worker, mine)
    results = []
    for i in order:
        o = openings[i]
        reasons, warning, dist = [], None, None
        s = {"match": match[i]}

        if None not in (worker["last_lat"], worker["last_lon"], o["latitude"], o["longitude"]):
            dist = km_between(worker["last_lat"], worker["last_lon"], o["latitude"], o["longitude"])
            s["distance"] = 1 / (1 + dist / 10)        # 0 km -> 1.0, 10 km -> 0.5, 30 km -> 0.25
            reasons.append(f"{round(dist)} km away")
        else:
            s["distance"] = 0.5

        if wage_usual:
            s["wage"] = min(1.0, max(0.0, 0.5 + 2.5 * (o["wage"] - wage_usual) / wage_usual))
            if o["wage"] > wage_usual:
                reasons.append(f"Rs. {o['wage']}, more than your usual Rs. {wage_usual}")
        else:
            s["wage"] = 0.5

        rec = find_record(records, o["contractor"])
        s["reliability"] = rec.score if rec else 0.5
        if rec and rec.days >= 5 and rec.unpaid_days == 0:
            reasons.append(f"{o['contractor']} paid all {rec.days} days workers logged")
        if rec and len(rec.workers_owed) >= 2:
            warning = f"{o['contractor']} still owes wages to {len(rec.workers_owed)} workers"

        mine_rec = find_record(my_records, o["contractor"])
        if mine_rec and mine_rec.unpaid_days:
            s["familiar"] = 0.0
            warning = f"{o['contractor']} still owes you for {mine_rec.unpaid_days} days"
        elif mine_rec and mine_rec.days:
            s["familiar"] = 1.0
            reasons.append(f"you worked with {o['contractor']} before and were paid")
        elif o["site_id"] and o["site_id"] == worker["site_id"]:
            s["familiar"] = 0.8
            reasons.append("at your current site")
        else:
            s["familiar"] = 0.5

        score = sum(weights[name] * s[name] for name in weights)
        results.append(Recommendation(o, score, s, reasons, warning, None if dist is None else round(dist)))

    results.sort(key=lambda r: r.score, reverse=True)
    top = results[:k]
    if log_results and top:
        db.log_recommendations(worker["id"], query, [(r.opening["id"], r.score) for r in top])
    return top
