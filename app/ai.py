"""Understanding worker messages with Gemini, with a rule-based fallback.

Design rule (same idea as an auditable pipeline): Gemini only *extracts* facts
from voice, text and photos into a fixed JSON schema. All money and eligibility
calculations happen in plain Python (records.py), so every number in a complaint
can be traced back to ledger entries.

For daily messages Gemini works as an agent: it decides which tools to call
(log_work, record_payment, get_balance, ...), the tools run in Python against the
ledger, and Gemini writes the reply from the tool results. A reply that mentions a
number no tool returned is thrown away and the tools' own fixed replies are sent.

Without GEMINI_API_KEY the app still runs: text messages go through a simple
English/Hinglish parser, so the project can be developed and tested offline.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

Intent = Literal[
    "register_details", "log_work", "record_payment", "ask_balance",
    "request_complaint", "request_registration", "report_death", "find_work", "cancel_last", "help", "unknown",
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


class MusterRow(BaseModel):
    name: str = Field(description="Worker's name as written in the register, in Latin script")
    present_days: list[int] = Field(default_factory=list,
                                    description="Day-of-month numbers (1-31) marked present for this worker")


class MusterSheet(BaseModel):
    is_muster_roll: bool = False
    month: Optional[int] = Field(None, description="Month 1-12 from the register header, if written")
    year: Optional[int] = Field(None, description="Four-digit year from the register header, if written")
    rows: list[MusterRow] = Field(default_factory=list)
    notes: Optional[str] = Field(None, description="Cells or names that could not be read")


MUSTER_PROMPT = """This is a photo of a paper attendance register (muster roll, haziri register) from a construction
site in India. Usually there is one row per worker and one column per day of the month. Present is marked with P,
a tick, 1, a signature or a thumb mark; absent with A, a dash, a cross or an empty cell. The register may be in
Hindi, English or another Indian language.

For each worker row return the name (romanised to Latin script) and the day numbers marked present.
Known workers at this site (if a written name is clearly one of these, spell it exactly like this): {known}.
Read the month and year from the header if they are written; otherwise leave them empty.
Never invent a row or a day. If a cell or a name cannot be read, leave it out and say so in notes.
If this is not an attendance register, set is_muster_roll to false and return no rows."""


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
- find_work: they are looking for new work or a job
- cancel_last: they say their last entry was wrong or ask to cancel / undo it
- help: greeting or asking what to do
Return the transcript in Latin script (romanised if needed)."""

BOARD_PROMPT = """This photo was sent by a construction worker in India. If it shows a project or site
board, extract the project name, the owner of the project (a government department such as PWD, CPWD,
NHAI, a municipal corporation or railways, or a private developer company), the contractor and location.
employer_type: 'government' for government departments and public bodies, 'developer' for private
companies, 'small' if it looks like a small private building or a house, 'unknown' if unclear.
If it is not a site board, set is_site_board to false."""


AGENT_PROMPT = """You are KaamPramaan, a WhatsApp assistant that keeps a record of work and wages for
daily-wage construction workers in India. Today is {today}. The worker is {name}.
The worker may speak Hindi, Bhojpuri, Bengali, Odia, Marathi, English or a mix.

Use the tools to act on the message:
- They worked (today or a recent day): call log_work. Leave wage empty if they did not say it.
- They received money: call record_payment.
- They ask how many days, how much is due, or their status: call get_balance.
- They say they were not paid, or ask for a complaint: call make_complaint.
- They ask about welfare board registration or their work record: call make_work_record.
- Someone reports that a worker died or was badly injured: call report_death.
- They say the last entry was wrong ("galat", "wrong", "cancel", "undo"): call void_last_entry. If they
  also give the correct values, call void_last_entry first, then log_work or record_payment.
- They are looking for new work: call find_work with what kind of work and where, if they said it.
  Tell them the top openings with wage, place and contact, and repeat any warning the tool gives.
One message can need more than one tool, for example work logged and money received.
Never guess a wage, amount or date that was not said. Pass a short transcript of what was said,
in Latin script, to log_work, record_payment and report_death.

After the tools run, reply in one to three short sentences, in the same language and script the
worker used. Only use numbers that appear in the tool results. If a tool returns an error, ask the
worker for what is missing. If no tool fits, tell them briefly how to log a day's work."""

_TRANSCRIPT = {"type": "string", "description": "What the worker said, in Latin script"}
_DATE = {"type": "string", "description": "ISO date YYYY-MM-DD of the work. Omit for today."}

TOOLS = [
    {
        "name": "log_work",
        "description": "Record one day of work for the worker.",
        "parameters": {
            "type": "object",
            "properties": {
                "work_date": _DATE,
                "contractor": {"type": "string", "description": "Contractor or thekedar name, if said"},
                "wage": {"type": "integer", "description": "That day's wage in rupees, only if said"},
                "paid": {"type": "boolean", "description": "True if that day's wage was already paid"},
                "transcript": _TRANSCRIPT,
            },
            "required": ["transcript"],
        },
    },
    {
        "name": "record_payment",
        "description": "Record money the worker received from a contractor.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "Rupees received"},
                "contractor": {"type": "string", "description": "Who paid, if said"},
                "transcript": _TRANSCRIPT,
            },
            "required": ["amount", "transcript"],
        },
    },
    {
        "name": "get_balance",
        "description": "Get days worked, money earned, received and still due, and welfare board eligibility.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "make_complaint",
        "description": "Create a wage complaint PDF listing every unpaid day.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "make_work_record",
        "description": "Create the pre-filled welfare board registration application with the 90-day work "
                       "certificate (PDF). Use when they ask about registration, labour card or the welfare board.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "find_work",
        "description": "Find work openings that suit this worker, ranked by fit, distance, wage and whether the "
                       "contractor pays.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Kind of work and place they asked for, in Latin script. Empty if not said."},
            },
        },
    },
    {
        "name": "void_last_entry",
        "description": "Cancel the worker's most recent work or payment entry (only works within 24 hours). "
                       "The cancelled entry stays on the record but no longer counts.",
        "parameters": {"type": "object", "properties": {"transcript": _TRANSCRIPT}},
    },
    {
        "name": "report_death",
        "description": "Open a case for an NGO volunteer when a worker has died or was badly injured.",
        "parameters": {
            "type": "object",
            "properties": {"worker_name": {"type": "string"}, "transcript": _TRANSCRIPT},
            "required": ["transcript"],
        },
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}
MAX_AGENT_STEPS = 4
RETRYABLE_CODES = (404, 429, 500, 502, 503, 504)
COOLDOWN_SECONDS = 300


@dataclass
class ToolCall:
    name: str
    args: dict
    result: dict


@dataclass
class AgentResult:
    text: str | None                     # None: the reply was rejected or never written
    calls: list[ToolCall] = field(default_factory=list)


def numbers_in(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", text.replace(",", ""))}


class AI:
    def __init__(self, api_key: str = "", model: str = "gemini-3.8-flash",
                 fallback_models: tuple[str, ...] = ("gemini-3.5-flash",),
                 tts_models: tuple[str, ...] = ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview"),
                 tts_voice: str = "Kore"):
        # Try the main model first; on overload (503), rate limit (429) or a retired model (404)
        # move to the next one, so a busy model does not stop workers from logging.
        self.models = [model] + [m for m in fallback_models if m and m != model]
        self._cooldown: dict[str, float] = {}  # model -> monotonic time it may be tried again
        self.model = model
        self.tts_models = [m for m in tts_models if m]
        self.tts_voice = tts_voice
        self.last_tts_model: str | None = None      # which TTS model produced the latest voice reply
        self.client = None
        if api_key:
            from google import genai  # imported lazily so tests do not need network setup
            from google.genai import types
            # Keep the SDK's own retries short; _generate() moves to the next model instead of waiting.
            self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
                timeout=12_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ))

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @property
    def can_call_tools(self) -> bool:
        return self.client is not None

    def _generate(self, contents, config=None):
        """Try models in order. A model that fails (busy, out of quota, timing out) is skipped for
        COOLDOWN_SECONDS, so one slow model cannot make every message wait for its timeout."""
        from google.genai import errors
        now = time.monotonic()
        healthy = [m for m in self.models if self._cooldown.get(m, 0) <= now]
        order = healthy or self.models  # if every model is cooling down, try them all once anyway
        last = None
        for m in order:
            try:
                resp = self.client.models.generate_content(model=m, contents=contents, config=config)
                self._cooldown.pop(m, None)
                return resp
            except errors.APIError as e:
                if getattr(e, "code", None) in RETRYABLE_CODES:
                    log.warning("Gemini model %s failed with %s, skipping it for %ss", m, e.code, COOLDOWN_SECONDS)
                    self._cooldown[m] = time.monotonic() + COOLDOWN_SECONDS
                    last = e
                    continue
                raise
            except Exception as e:  # network timeouts surface as httpx errors, not APIError
                if "timeout" not in type(e).__name__.lower() and "timed out" not in str(e).lower():
                    raise
                log.warning("Gemini model %s timed out, skipping it for %ss", m, COOLDOWN_SECONDS)
                self._cooldown[m] = time.monotonic() + COOLDOWN_SECONDS
                last = e
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

    # ---------- agent with tools ----------
    def run_agent(self, execute: Callable[[str, dict], dict], text: str | None = None,
                  audio: bytes | None = None, audio_mime: str = "audio/ogg",
                  worker_name: str | None = None, today: date | None = None) -> AgentResult:
        """Let Gemini call tools until it writes a reply.

        execute(name, args) runs one tool and returns a JSON-safe dict. If Gemini fails
        before any tool ran, the error is raised so the caller can use the intent parser.
        If it fails after a tool ran, the calls made so far are returned with text=None,
        so nothing is logged twice.
        """
        from google.genai import types
        today = today or date.today()
        config = types.GenerateContentConfig(
            system_instruction=AGENT_PROMPT.format(today=today.isoformat(), name=worker_name or "unknown"),
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["parameters"]) for t in TOOLS])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0,
        )
        parts = []
        if audio:
            parts.append(types.Part.from_bytes(data=audio, mime_type=audio_mime))
        if text:
            parts.append(types.Part.from_text(text=text))
        contents = [types.Content(role="user", parts=parts)]
        calls: list[ToolCall] = []

        for _ in range(MAX_AGENT_STEPS):
            try:
                resp = self._generate(contents, config)
            except Exception:
                if not calls:
                    raise
                log.exception("Gemini failed after tools ran, sending fixed replies")
                return AgentResult(None, calls)

            requested = resp.function_calls or []
            if not requested:
                return AgentResult(self._checked_reply(resp.text, calls), calls)

            contents.append(resp.candidates[0].content)
            answers = []
            for fc in requested:
                args = dict(fc.args or {})
                if fc.name not in TOOL_NAMES:
                    result = {"error": f"unknown tool {fc.name}"}
                else:
                    result = execute(fc.name, args)
                    calls.append(ToolCall(fc.name, args, result))
                answers.append(types.Part.from_function_response(name=fc.name, response=result))
            contents.append(types.Content(role="user", parts=answers))

        log.warning("Agent stopped after %d steps without a reply", MAX_AGENT_STEPS)
        return AgentResult(None, calls)

    @staticmethod
    def _checked_reply(text: str | None, calls: list[ToolCall]) -> str | None:
        """Keep Gemini's reply only if every number in it came from a tool call or its result."""
        out = (text or "").strip()
        if not out:
            return None
        allowed = numbers_in(json.dumps([[c.args, c.result] for c in calls], ensure_ascii=False))
        if not numbers_in(out) <= allowed:
            log.warning("Agent reply used a number no tool returned, sending fixed replies: %r", out)
            return None
        return out

    # ---------- voice replies ----------
    def speak(self, text: str) -> bytes | None:
        """Read a short reply aloud with Gemini text-to-speech. Returns WAV bytes, or None if every TTS
        model fails, so callers can fall back to the text reply."""
        if not self.client or not (text or "").strip():
            return None
        from google.genai import types

        from .voice import pcm_to_wav, rate_from_mime
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.tts_voice))),
        )
        for model in self.tts_models:
            try:
                resp = self.client.models.generate_content(model=model, contents=text, config=config)
                blob = resp.candidates[0].content.parts[0].inline_data
                if blob and blob.data:
                    self.last_tts_model = model
                    return pcm_to_wav(blob.data, rate=rate_from_mime(getattr(blob, "mime_type", None)))
            except Exception:
                log.exception("Gemini TTS model %s failed, trying the next one", model)
        return None

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

    # ---------- muster register photos ----------
    def read_muster(self, image: bytes, mime: str = "image/jpeg", known_names: list[str] | None = None) -> MusterSheet:
        """Gemini vision reads a handwritten attendance register into rows. The result is only a draft:
        app/muster.py range-checks it and a person reviews it before anything is saved."""
        if not self.client:
            return MusterSheet(is_muster_roll=False, notes="Reading register photos needs GEMINI_API_KEY")
        from google.genai import types
        known = ", ".join(sorted(set(known_names or []))[:200]) or "none yet"
        try:
            resp = self._generate(
                [MUSTER_PROMPT.format(known=known), types.Part.from_bytes(data=image, mime_type=mime)],
                types.GenerateContentConfig(
                    response_mime_type="application/json", response_schema=MusterSheet, temperature=0),
            )
            return resp.parsed if isinstance(resp.parsed, MusterSheet) else MusterSheet(**json.loads(resp.text))
        except Exception:
            log.exception("Gemini muster reading failed")
            return MusterSheet(is_muster_roll=False, notes="Could not read the photo, please try a clearer one")

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
    elif _has(t, ["galat", "ghalat", "wrong", "cancel", "undo", "mistake", "delete last", "last entry"]):
        u.intent = "cancel_last"
    elif _has(t, ["not paid me", "hasn't paid", "has not paid", "didn't pay", "did not pay", "nahi de raha",
                  "nahi diya", "paisa nahi", "complaint", "shikayat"]):
        u.intent = "request_complaint"
        u.contractor = _contractor(text)
    elif _has(t, ["kaam chahiye", "kam chahiye", "kaam chaiye", "need work", "need a job", "looking for work",
                  "find work", "find me work", "any work", "naukri", "kaam dhund", "kaam milega", "job chahiye",
                  "koi kaam", "work chahiye", "new work", "naya kaam"]):
        u.intent = "find_work"
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
