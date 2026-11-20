"""Paper muster register -> verified working days.

Most sites already keep a paper attendance register. A supervisor (on WhatsApp) or an officer (on the website)
sends a photo; Gemini reads it into rows (app/ai.py read_muster); this module range-checks the rows; a person
reviews them on /muster/<id>; only then are they saved, each day as a ledger entry vouched for by the register.
"""
import calendar
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import attest, ledger
from .ai import AI, MusterSheet
from .db import Database
from .records import live_entries

MAX_ROWS = 200


def clean_sheet(sheet: MusterSheet, today: date) -> dict:
    """Keep only what can be true: a month in the last year, real day numbers, no future days, no blank names."""
    month = sheet.month if sheet.month and 1 <= sheet.month <= 12 else today.month
    year = sheet.year if sheet.year and today.year - 1 <= sheet.year <= today.year else today.year
    if date(year, month, 1) > today:          # e.g. "12" read for a register from last December
        year -= 1
    if date(year, month, 1) < (today.replace(day=1) - timedelta(days=366)):
        month, year = today.month, today.year
    last_day = calendar.monthrange(year, month)[1]
    rows, dropped = [], 0
    for r in sheet.rows[:MAX_ROWS]:
        name = re.sub(r"\s+", " ", (r.name or "")).strip(" .,-").title()
        days = sorted({d for d in r.present_days if 1 <= d <= last_day and date(year, month, d) <= today})
        dropped += len(set(r.present_days)) - len(days)
        if name and days:
            rows.append({"name": name, "days": days})
    notes = (sheet.notes or "").strip()
    if dropped:
        notes = (notes + " " if notes else "") + f"{dropped} day mark(s) were outside the month or in the future and were left out."
    return {"is_muster_roll": bool(sheet.is_muster_roll), "month": month, "year": year, "rows": rows,
            "notes": notes or None}


def read_photo(db: Database, ai: AI, site, image: bytes, mime: str, media_dir: str, uploaded_by: str,
               today: date) -> tuple[int, dict]:
    """Save the photo, let Gemini read it, store the cleaned result as a draft. Returns (import id, draft)."""
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
    os.makedirs(media_dir, exist_ok=True)
    path = os.path.join(media_dir, f"muster_{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(image)
    with db.connect() as c:
        known = [r["name"] for r in c.execute("SELECT name FROM workers WHERE site_id = ? AND name IS NOT NULL",
                                               (site["id"],))]
    draft = clean_sheet(ai.read_muster(image, mime, known), today)
    import_id = db.create_muster_import(site["id"], path, ledger.sha256_bytes(image), json.dumps(draft), uploaded_by)
    return import_id, draft


def parse_days(text: str, last_day: int) -> list[int]:
    """'1, 2, 5-9' -> [1, 2, 5, 6, 7, 8, 9]; anything outside 1..last_day is dropped."""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", text or ""):
        m = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            out.update(d for d in range(min(a, b), max(a, b) + 1) if 1 <= d <= last_day)
    return sorted(out)


@dataclass
class ImportResult:
    workers: int = 0
    new_workers: int = 0
    new_days: int = 0
    confirmed_existing: int = 0
    skipped_future: int = 0
    names: list[str] = field(default_factory=list)


def import_rows(db: Database, imp, rows: list[dict], month: int, year: int, reviewer: str,
                today: date) -> ImportResult:
    """Save reviewed rows. A day the worker already logged gets vouched for; a missing day is added as an
    entry from the register and vouched for. Every attestation carries the photo's SHA-256."""
    site = db.get_site(imp["site_id"])
    attester = f"Muster register photo #{imp['id']} (reviewed by {reviewer})"
    res = ImportResult()
    for row in rows:
        name = row["name"].strip().title()
        if not name or not row["days"]:
            continue
        w = db.find_worker_at_site(site["id"], name)
        if w is None:
            wid = db.create_worker(None, name=name, state="active", site_id=site["id"])
            res.new_workers += 1
        else:
            wid = w["id"]
        res.workers += 1
        res.names.append(name)
        live = live_entries(db.list_entries(wid))
        to_vouch = []
        for d in row["days"]:
            day = date(year, month, d)
            if day > today:
                res.skipped_future += 1
                continue
            iso = day.isoformat()
            existing = next((e for e in live if e["kind"] == "work" and e["work_date"] == iso
                             and e["site_id"] == site["id"]), None)
            if existing:
                to_vouch.append(existing)
                res.confirmed_existing += 1
                continue
            to_vouch.append(ledger.append(db, ledger.NewEntry(
                worker_id=wid, kind="work", work_date=iso, amount=0, source="muster_photo", site_id=site["id"],
                contractor=site["contractor"], transcript=f"Present in muster register (photo #{imp['id']})",
                media_path=imp["photo_path"], media_sha256=imp["photo_sha256"])))
            res.new_days += 1
        attest.attest(db, to_vouch, "muster_register", attester, evidence_sha256=imp["photo_sha256"])
    db.close_muster_import(imp["id"], "imported", reviewer)
    return res
