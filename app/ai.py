"""Understanding worker messages with Gemini, with a rule-based fallback.

Design rule (same idea as an auditable pipeline): Gemini only *extracts* facts
from voice, text and photos into a fixed JSON schema. All money and eligibility
calculations happen in plain Python (records.py), so every number in a complaint
can be traced back to ledger entries.

Without GEMINI_API_KEY the app still runs: text messages go through a simple
English/Hinglish parser, so the project can be developed and tested offline.
"""
import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

Intent = Literal[
    "register_details", "log_work", "record_payment", "ask_balance",
    "request_complaint", "request_registration", "report_death", "help", "unknown",
]


class Understanding(BaseModel):
    intent: Intent = "unknown"
    transcript: str = Field("", description="What the person said, in their own words, in Latin script")
    name: Optional[str] = None
    village: Optional[str] = None
    work_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD of the work, if mentioned")
    contractor: Optional[str] = None
    wage: Optional[int] = Field(None, description="Day's wage in rupees for log_work")
    paid: Optional[bool] = Field(None, description="For log_work: was today's wage already paid")
    amount: Optional[int] = Field(None, description="Rupees received, for record_payment")
    worker_name: Optional[str] = Field(None, description="For report_death: name of the worker")


class BoardInfo(BaseModel):
    is_site_board: bool = False
    project: Optional[str] = None
    principal_employer: Optional[str] = Field(None, description="Government department or developer company that owns the project")
    employer_type: Literal["government", "developer", "small", "unknown"] = "unknown"
    contractor: Optional[str] = None
    location: Optional[str] = None


UNDERSTAND_PROMPT = """You help daily-wage construction workers in India keep a record of their work.
Today is {today}. The worker may speak Hindi, Bhojpuri, Bengali, Odia, Marathi, English or a mix.
Classify the message and extract facts. Do not guess numbers that were not said.

intents:
- register_details: they are telling their name and village (expected step: {expected})
- log_work: they worked today or on a given day (extract contractor, wage, whether paid)
- record_payment: they received money (extract amount)
- ask_balance: they ask how many days, how much is due, or their status
- request_complaint: they say they were not paid, or ask for a complaint
- request_registration: they ask about welfare board registration
- report_death: someone reports that a worker died or was badly injured
- help: greeting or asking what to do
Return the transcript in Latin script (romanised if needed)."""

BOARD_PROMPT = """This photo was sent by a construction worker in India. If it shows a project or site
board, extract the project name, the owner of the project (a government department such as PWD, CPWD,
NHAI, a municipal corporation or railways, or a private developer company), the contractor and location.
employer_type: 'government' for government departments and public bodies, 'developer' for private
companies, 'small' if it looks like a small private building or a house, 'unknown' if unclear.
If it is not a site board, set is_site_board to false."""


class AI:
    def __init__(self, api_key: str = "", model: str = "gemini-3.8-flash",
                 fallback_models: tuple[str, ...] = ("gemini-3.5-flash",)):
        # Try the main model first; on overload (503), rate limit (429) or a retired model (404)
        # move to the next one, so a busy model does not stop workers from logging.
        self.models = [model] + [m for m in fallback_models if m and m != model]
        self.model = model
        self.client = None
        if api_key:
            from google import genai  # imported lazily so tests do not need network setup
            from google.genai import types
            # Keep the SDK's own retries short; _generate() moves to the next model instead of waiting.
            self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
                timeout=30_000,
                retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1, max_delay=4),
            ))

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _generate(self, contents, config=None):
        from google.genai import errors
        last = None
        for attempt in range(2):  # two passes over the model list, short pause in between
            if attempt:
                time.sleep(2)
            for m in self.models:
                try:
                    return self.client.models.generate_content(model=m, contents=contents, config=config)
                except errors.APIError as e:
                    if getattr(e, "code", None) in (404, 429, 500, 503):
                        log.warning("Gemini model %s failed with %s, trying next model", m, e.code)
                        last = e
                        continue
                    raise
        raise last

    # ---------- message understanding ----------
    def understand(self, text: str | None = None, audio: bytes | None = None,
                   audio_mime: str = "audio/ogg", expected: str = "none",
                   today: date | None = None) -> Understanding:
        today = today or date.today()
        if self.client and (text or audio):
            try:
                return self._understand_gemini(text, audio, audio_mime, expected, today)
            except Exception:  # network errors, quota, bad JSON: fall back rather than lose the message
                log.exception("Gemini understanding failed, using fallback parser")
        if text:
            return parse_text(text, expected=expected, today=today)
        return Understanding(intent="unknown", transcript="")

    def _understand_gemini(self, text, audio, audio_mime, expected, today) -> Understanding:
        from google.genai import types
        parts = [UNDERSTAND_PROMPT.format(today=today.isoformat(), expected=expected)]
        if audio:
            parts.append(types.Part.from_bytes(data=audio, mime_type=audio_mime))
        if text:
            parts.append(f"Message: {text}")
        resp = self._generate(
            parts,
            types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Understanding,
                temperature=0,
            ),
        )
        result = resp.parsed if isinstance(resp.parsed, Understanding) else Understanding(**json.loads(resp.text))
        return _clean(result, today)

    # ---------- site board photos ----------
    def read_board(self, image: bytes, mime: str = "image/jpeg") -> BoardInfo:
        if not self.client:
            return BoardInfo(is_site_board=False)
        from google.genai import types
        try:
            resp = self._generate(
                [BOARD_PROMPT, types.Part.from_bytes(data=image, mime_type=mime)],
                types.GenerateContentConfig(
                    response_mime_type="application/json", response_schema=BoardInfo, temperature=0),
            )
            return resp.parsed if isinstance(resp.parsed, BoardInfo) else BoardInfo(**json.loads(resp.text))
        except Exception:
            log.exception("Gemini board reading failed")
            return BoardInfo(is_site_board=False)

    # ---------- complaint wording ----------
    def complaint_paragraph(self, facts: dict) -> str:
        """Gemini may only reword facts we already computed; the numbers come from `facts`."""
        fallback = default_complaint_paragraph(facts)
        if not self.client:
            return fallback
        prompt = (
            "Write one short, formal paragraph (max 120 words) for a wage complaint to a Labour Officer in India. "
            "Use only these facts, do not add any numbers or claims that are not here, no legal section numbers:\n"
            + json.dumps(facts, ensure_ascii=False)
        )
        try:
            resp = self._generate(prompt)
            out = (resp.text or "").strip()
            # Guard: every number in the output must appear in the facts.
            allowed = {int(n) for n in re.findall(r"\d+", json.dumps(facts).replace(",", ""))}
            used = {int(n) for n in re.findall(r"\d+", out.replace(",", ""))}
            return out if out and used <= allowed else fallback
        except Exception:
            log.exception("Gemini complaint wording failed")
            return fallback


def default_complaint_paragraph(f: dict) -> str:
    return (
        f"I, {f['worker_name']}, worked as a construction worker at {f['site']} under contractor "
        f"{f['contractor']}. I worked {f['unpaid_days']} days between {f['from_date']} and {f['to_date']} "
        f"for which I have not been paid. The total amount due is Rs. {f['due']}. I recorded my working days "
        f"through KaamPramaan as I worked, and the attached record lists every day with its date, wage and "
        f"a tamper-proof reference code. I request you to direct the contractor and the principal employer, "
        f"{f['principal_employer']}, to pay my pending wages."
    )


def _clean(u: Understanding, today: date) -> Understanding:
    if u.work_date:
        try:
            d = date.fromisoformat(u.work_date)
            if d > today or d < today - timedelta(days=60):
                u.work_date = today.isoformat()
        except ValueError:
            u.work_date = None
    for attr in ("wage", "amount"):
        v = getattr(u, attr)
        if v is not None and not (0 < v <= 100000):
            setattr(u, attr, None)
    if u.contractor:
        u.contractor = u.contractor.strip().title()
    return u


# ---------------- rule-based fallback ----------------
_AMOUNT = re.compile(r"(?:rs\.?|₹|inr)?\s*(\d{1,3}(?:,\d{2,3})+|\d{2,6})\s*(?:rs|rupees|rupaye|rupay|rupee|/-)?", re.I)
_CONTRACTOR = [
    re.compile(r"\b(?:with|for|under)\s+([A-Z][a-zA-Z]+)", re.I),
    re.compile(r"\b([A-Za-z]+)\s+(?:thekedar|thekedaar|contractor|seth|ke\s+saath|ke\s+sath|ke\s+yahan)", re.I),
]
_NOT_CONTRACTOR = {"today", "aaj", "kal", "me", "mujhe", "work", "kaam", "site", "the", "my", "rs", "him", "worked"}


def _amount(text: str) -> int | None:
    for m in _AMOUNT.finditer(text):
        v = int(m.group(1).replace(",", ""))
        if 0 < v <= 100000:
            return v
    return None


def _contractor(text: str) -> str | None:
    for rx in _CONTRACTOR:
        m = rx.search(text)
        if m and m.group(1).lower() not in _NOT_CONTRACTOR:
            return m.group(1).title()
    return None


def _has(t: str, words) -> bool:
    return any(w in t for w in words)


def parse_text(text: str, expected: str = "none", today: date | None = None) -> Understanding:
    today = today or date.today()
    t = " " + text.lower().strip() + " "
    u = Understanding(transcript=text.strip())

    if expected == "name":
        cleaned = re.sub(r"^(my name is|i am|mera naam|naam)\s+", "", text.strip(), flags=re.I)
        cleaned = re.sub(r"\s+(hai|he)\b", "", cleaned, flags=re.I)
        parts = re.split(r",|\bfrom\b|\bgaon\b|\bvillage\b|\bse\b", cleaned, maxsplit=1, flags=re.I)
        u.intent = "register_details"
        u.name = parts[0].strip(" .").title() or None
        u.village = parts[1].strip(" .").title() if len(parts) > 1 and parts[1].strip() else None
        return u

    worked = _has(t, ["worked", "kaam kiya", "work today", "aaj kaam", "duty", "kaam pe", "kaam par"])
    if _has(t, ["died", "death", " dead", "maut", "guzar gay", "expired", "mar gay", "accident"]):
        u.intent = "report_death"
    elif _has(t, ["not paid me", "hasn't paid", "has not paid", "didn't pay", "did not pay", "nahi de raha",
                  "nahi diya", "paisa nahi", "complaint", "shikayat"]):
        u.intent = "request_complaint"
        u.contractor = _contractor(text)
    elif worked:
        u.intent = "log_work"
        u.wage = _amount(text)
        u.contractor = _contractor(text)
        if _has(t, ["not paid", "baaki", "nahi mila", " due", "pending", "abhi nahi"]):
            u.paid = False
        elif _has(t, [" paid", "mil gaya", "mila "]):
            u.paid = True
        u.work_date = (today - timedelta(days=1)).isoformat() if _has(t, ["yesterday", " kal "]) else today.isoformat()
    elif _has(t, ["welfare", "registration", "register me", "board card", "labour card"]):
        u.intent = "request_registration"
    elif _has(t, ["paid me", "gave me", "received", " diye", "de diya", "mil gaya", "mile ", "payment"]):
        u.intent = "record_payment"
        u.amount = _amount(text)
        u.contractor = _contractor(text)
    elif _has(t, ["how many days", "kitne din", "balance", "kitna baaki", "how much", "status", "hisaab"]):
        u.intent = "ask_balance"
    elif _has(t, [" hi ", "hello", "namaste", "help", "madad", "start"]):
        u.intent = "help"
    return u
