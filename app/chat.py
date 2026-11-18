"""The conversation a worker has with KaamPramaan. Independent of WhatsApp so the
same logic serves WhatsApp, the web simulator and (later) a missed-call voice line."""
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date

from . import ledger, pdfs
from .ai import AI, BoardInfo, Understanding, _clean
from .db import Database
from .records import rupees, summarise

log = logging.getLogger(__name__)


@dataclass
class Inbound:
    phone: str
    text: str | None = None
    audio: bytes | None = None
    audio_mime: str = "audio/ogg"
    image: bytes | None = None
    image_mime: str = "image/jpeg"
    latitude: float | None = None
    longitude: float | None = None
    source: str = "whatsapp"


@dataclass
class Reply:
    text: str
    document_url: str | None = None
    document_name: str | None = None


@dataclass
class ToolOutcome:
    """What a tool tells Gemini (data) and the fixed reply sent when Gemini's reply is not used."""
    data: dict
    reply: Reply


@dataclass
class Bot:
    db: Database
    ai: AI
    media_dir: str
    public_base_url: str
    today_fn: callable = field(default=date.today)

    # ------------------------------------------------------------------ entry point
    def handle(self, msg: Inbound) -> list[Reply]:
        worker = self.db.get_worker_by_phone(msg.phone)
        if worker is None:
            self.db.create_worker(msg.phone, state="awaiting_name")
            return [Reply(
                "Namaste! KaamPramaan aapke kaam ka record rakhta hai.\n"
                "Welcome! KaamPramaan keeps a record of your work.\n\n"
                "Apna naam aur gaon bhejiye (voice ya text).\n"
                "Please send your name and village (voice note or text)."
            )]

        if msg.latitude is not None and msg.longitude is not None:
            self.db.update_worker(worker["id"], last_lat=msg.latitude, last_lon=msg.longitude)
            if not (msg.text or msg.audio or msg.image):
                return [Reply("Location saved. Aapki location save ho gayi.")]

        if msg.audio and not msg.text and not self.ai.enabled:
            return [Reply("Voice notes need Gemini to be set up (GEMINI_API_KEY). Please type the message for now.")]

        state = worker["state"]
        if state == "awaiting_name":
            return self._on_name(worker, msg)
        if state == "awaiting_site":
            return self._on_site(worker, msg)
        if msg.image:
            return self._on_board_photo(worker, msg, registering=False)
        return self._on_active(worker, msg)

    # ------------------------------------------------------------------ registration
    def _on_name(self, worker, msg):
        u = self.ai.understand(text=msg.text, audio=msg.audio, audio_mime=msg.audio_mime,
                               expected="name", today=self.today_fn())
        if not u.name:
            return [Reply("Naam samajh nahi aaya. Please send your name and village again, e.g. "
                          "\"Raju Kumar, Bhojpur\".")]
        self.db.update_worker(worker["id"], name=u.name, village=u.village, state="awaiting_site")
        return [Reply(
            f"Dhanyavaad, {u.name}{' (' + u.village + ')' if u.village else ''}.\n\n"
            "Ab apni site ke board ki photo bhejiye. Agar board nahi hai to \"skip\" likhiye.\n"
            "Now send a photo of your site's project board. If there is no board, send \"skip\"."
        )]

    def _on_site(self, worker, msg):
        if msg.image:
            return self._on_board_photo(worker, msg, registering=True)
        if (msg.text or "").strip().lower() in {"skip", "no board", "nahi hai", "board nahi"}:
            site_id = self.db.create_site(project="Unnamed site", employer_type="small")
            self.db.update_worker(worker["id"], site_id=site_id, state="active")
            return [Reply("Theek hai. Site saved without a board.\n\n" + HOW_TO_LOG)]
        return [Reply("Please send a photo of the site board, or send \"skip\".")]

    def _on_board_photo(self, worker, msg, registering: bool):
        photo_path, _ = self._save_media(msg.image, msg.image_mime)
        if self.ai.enabled:
            info = self.ai.read_board(msg.image, msg.image_mime)
        else:  # offline mode: keep the photo, owner details can be filled in from the website
            info = BoardInfo(is_site_board=True, project="Site (board photo saved)", employer_type="unknown")
        if not info.is_site_board:
            if registering:
                return [Reply("Yeh board ki photo nahi lag rahi. This does not look like a site board. "
                              "Please send a clear photo of the board, or send \"skip\".")]
            return [Reply("Photo mili, lekin yeh site board nahi hai. Photo received. To log work, send a voice note.")]
        existing = self.db.find_site(info.project or "", info.contractor)
        site_id = existing["id"] if existing else self.db.create_site(
            project=info.project, principal_employer=info.principal_employer,
            employer_type=info.employer_type, contractor=info.contractor,
            location=info.location, board_photo=photo_path,
        )
        self.db.update_worker(worker["id"], site_id=site_id, state="active")
        who = {
            "government": f"Malik (principal employer): {info.principal_employer} - sarkari project.",
            "developer": f"Malik (principal employer): {info.principal_employer} - private developer.",
        }.get(info.employer_type, "Small private site.")
        text = (f"Site saved: {info.project or 'site'}.\n"
                + (f"Location: {info.location}.\n" if info.location else "")
                + f"Contractor: {info.contractor or 'not on board'}.\n{who}")
        return [Reply(text + ("\n\n" + HOW_TO_LOG if registering else ""))]

    # ------------------------------------------------------------------ daily use
    def _on_active(self, worker, msg):
        media = (None, None)
        if msg.audio:
            media = self._save_media(msg.audio, msg.audio_mime)

        if self.ai.can_call_tools:
            replies = self._run_agent(worker, msg, media)
            if replies is not None:
                return replies

        u = self.ai.understand(text=msg.text, audio=msg.audio, audio_mime=msg.audio_mime,
                               expected="none", today=self.today_fn())
        tool = INTENT_TO_TOOL.get(u.intent)
        if tool is None:
            return [Reply(HOW_TO_LOG)]
        return [self._call_tool(worker, msg, media, tool, u).reply]

    def _run_agent(self, worker, msg, media) -> list[Reply] | None:
        """Gemini picks the tools. Returns None if Gemini failed before any tool ran,
        so the caller can fall back to the intent parser."""
        outcomes: list[ToolOutcome] = []

        def execute(name: str, args: dict) -> dict:
            u = Understanding(
                intent=TOOL_TO_INTENT[name], transcript=str(args.get("transcript") or ""),
                work_date=args.get("work_date"), contractor=args.get("contractor"),
                wage=_int_or_none(args.get("wage")), paid=args.get("paid"),
                amount=_int_or_none(args.get("amount")), worker_name=args.get("worker_name"),
            )
            current = self.db.get_worker(worker["id"])  # last_wage may change within one message
            outcome = self._call_tool(current, msg, media, name, u)
            outcomes.append(outcome)
            return outcome.data

        try:
            result = self.ai.run_agent(execute, text=msg.text, audio=msg.audio, audio_mime=msg.audio_mime,
                                       worker_name=worker["name"], today=self.today_fn())
        except Exception:
            log.exception("Agent failed before any tool ran, using the intent parser")
            return None

        docs = [o.reply for o in outcomes if o.reply.document_url]
        if result.text:
            if docs:  # attach the PDF to Gemini's reply
                return [Reply(result.text, docs[-1].document_url, docs[-1].document_name)]
            return [Reply(result.text)]
        if outcomes:
            return [o.reply for o in outcomes]
        return [Reply(HOW_TO_LOG)]

    def _call_tool(self, worker, msg, media, name: str, u: Understanding) -> ToolOutcome:
        u = _clean(u, self.today_fn())
        if name == "log_work":
            return self._log_work(worker, msg, media, u)
        if name == "record_payment":
            return self._record_payment(worker, msg, media, u)
        if name == "get_balance":
            return self._balance(worker)
        if name == "make_complaint":
            return self._complaint(worker)
        if name == "make_work_record":
            return self._registration(worker)
        if name == "report_death":
            return self._report_death(msg, u)
        raise ValueError(f"unknown tool {name}")

    # ------------------------------------------------------------------ tools
    def _log_work(self, worker, msg, media, u: Understanding) -> ToolOutcome:
        today = self.today_fn()
        wage = u.wage or worker["last_wage"]
        if not wage:
            return ToolOutcome(
                {"saved": False, "error": "wage missing, ask the worker for the day's wage"},
                Reply("Aaj ki mazdoori kitni thi? How much was today's wage? "
                      "Please send again with the amount, e.g. \"Worked with Ramesh, 700\"."))
        site = self.db.get_site(worker["site_id"])
        work_date = u.work_date or today.isoformat()
        entry = ledger.append(self.db, ledger.NewEntry(
            worker_id=worker["id"], kind="work", work_date=work_date,
            amount=wage, source=msg.source, site_id=worker["site_id"],
            contractor=u.contractor or (site["contractor"] if site else None),
            paid=bool(u.paid), transcript=u.transcript or msg.text,
            media_path=media[0], media_sha256=media[1],
            latitude=worker["last_lat"], longitude=worker["last_lon"],
        ))
        self.db.update_worker(worker["id"], last_wage=wage)
        s = self._summary(worker["id"])
        d = date.fromisoformat(work_date)
        return ToolOutcome(
            {"saved": True, "work_date": work_date, "wage": wage, "paid": bool(u.paid),
             "contractor": entry["contractor"], "days_this_month": s.days_this_month, "due": s.due},
            Reply(f"Saved: {d.strftime('%d %b')}, Rs. {rupees(wage)} ({'paid' if u.paid else 'not paid yet'}).\n"
                  f"Is mahine / This month: {days(s.days_this_month)}. Baaki / Due: Rs. {rupees(s.due)}."))

    def _record_payment(self, worker, msg, media, u: Understanding) -> ToolOutcome:
        if not u.amount:
            return ToolOutcome(
                {"saved": False, "error": "amount missing, ask how much was received"},
                Reply("Kitne paise mile? How much were you paid? e.g. \"Ramesh paid me 5000\"."))
        ledger.append(self.db, ledger.NewEntry(
            worker_id=worker["id"], kind="payment", work_date=self.today_fn().isoformat(), amount=u.amount,
            source=msg.source, site_id=worker["site_id"], contractor=u.contractor,
            transcript=u.transcript or msg.text, media_path=media[0], media_sha256=media[1],
        ))
        s = self._summary(worker["id"])
        return ToolOutcome(
            {"saved": True, "amount": u.amount, "still_due": s.due},
            Reply(f"Payment noted: Rs. {rupees(u.amount)}. Baaki / Still due: Rs. {rupees(s.due)}."))

    def _balance(self, worker) -> ToolOutcome:
        s = self._summary(worker["id"])
        elig = ("Aap welfare board registration ke liye eligible hain. You can register with the welfare board. "
                "Send \"registration\" to get your work record.") if s.welfare_eligible else (
            f"Welfare board ke liye {s.days_to_eligibility} din aur chahiye. "
            f"{s.days_to_eligibility} more days needed for welfare board registration.")
        return ToolOutcome(
            {"days_worked": s.days_worked, "days_last_12_months": s.days_last_12_months,
             "days_this_month": s.days_this_month, "earned": s.earned, "received": s.paid, "due": s.due,
             "welfare_eligible": s.welfare_eligible, "days_to_welfare_eligibility": s.days_to_eligibility},
            Reply(f"Total days worked: {s.days_worked}. Last 12 months: {s.days_last_12_months}.\n"
                  f"Earned Rs. {rupees(s.earned)}, received Rs. {rupees(s.paid)}, due Rs. {rupees(s.due)}.\n{elig}"))

    def _report_death(self, msg, u: Understanding) -> ToolOutcome:
        details = u.transcript or msg.text
        if u.worker_name:
            details = f"{u.worker_name}: {details}"
        case_id = self.db.create_case("death_report", reporter_phone=msg.phone, worker_id=None, details=details)
        return ToolOutcome(
            {"case_opened": True, "next_step": "an NGO volunteer will call; keep the worker's Aadhaar ready",
             "case_id": case_id},
            Reply("Hum aapke saath hain. We are very sorry.\n"
                  "Ek NGO volunteer aapse jaldi sampark karega. An NGO volunteer will contact you soon to help "
                  "with the claim. Please keep the worker's Aadhaar and phone number ready."))

    # ------------------------------------------------------------------ documents
    def _complaint(self, worker) -> ToolOutcome:
        s = self._summary(worker["id"])
        if s.due <= 0:
            return ToolOutcome({"created": False, "due": 0, "reason": "no unpaid wages in the record"},
                               Reply("Aapka koi paisa baaki nahi hai. Our record shows no unpaid wages."))
        site = self.db.get_site(worker["site_id"])
        chain = ledger.verify_chain(self.db, worker["id"])
        corr = self.db.corroboration_counts(worker["id"])
        facts = {
            "worker_name": worker["name"], "site": (site["project"] if site else None) or "the site",
            "contractor": next((u.contractor for u in s.unpaid_days if u.contractor), "the contractor"),
            "principal_employer": (site["principal_employer"] if site else None) or "the principal employer",
            "unpaid_days": len(s.unpaid_days), "from_date": s.unpaid_days[0].work_date,
            "to_date": s.unpaid_days[-1].work_date, "due": rupees(s.due),
        }
        paragraph = self.ai.complaint_paragraph(facts)
        url = self._make_doc(worker, "complaint", lambda path: pdfs.complaint_pdf(
            path, worker, site, s, chain, corr, paragraph, today=self.today_fn()))
        return ToolOutcome(
            {"created": True, "due": s.due, "unpaid_days": len(s.unpaid_days),
             "from_date": facts["from_date"], "to_date": facts["to_date"], "pdf_attached": True,
             "next_step": "show the complaint to the labour office or an NGO"},
            Reply(f"Aapke Rs. {rupees(s.due)} baaki hain ({len(s.unpaid_days)} din). "
                  f"You are owed Rs. {rupees(s.due)} for {days(len(s.unpaid_days))}.\n"
                  "Yeh shikayat labour office ya NGO ko dikhaiye. Show this complaint to the labour office or an NGO.",
                  document_url=url, document_name="KaamPramaan_complaint.pdf"))

    def _registration(self, worker) -> ToolOutcome:
        entries = self.db.list_entries(worker["id"])
        s = summarise(entries, today=self.today_fn())
        site = self.db.get_site(worker["site_id"])
        chain = ledger.verify_chain(self.db, worker["id"])
        url = self._make_doc(worker, "registration", lambda path: pdfs.registration_pdf(
            path, worker, site, entries, s, chain, today=self.today_fn()))
        head = (f"Aapke {s.days_last_12_months} din record hain. You have {s.days_last_12_months} days recorded in "
                "the last 12 months.")
        tail = (" This meets the 90-day rule for welfare board registration." if s.welfare_eligible
                else f" You need {s.days_to_eligibility} more days for registration.")
        return ToolOutcome(
            {"created": True, "days_last_12_months": s.days_last_12_months, "days_required": 90,
             "welfare_eligible": s.welfare_eligible, "days_to_welfare_eligibility": s.days_to_eligibility,
             "pdf_attached": True},
            Reply(head + tail, document_url=url, document_name="KaamPramaan_work_record.pdf"))

    # ------------------------------------------------------------------ helpers
    def _summary(self, worker_id):
        return summarise(self.db.list_entries(worker_id), today=self.today_fn())

    def _make_doc(self, worker, kind, render) -> str:
        token = secrets.token_urlsafe(16)
        folder = os.path.join(self.media_dir, "docs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{token}.pdf")
        render(path)
        self.db.save_document(token, worker["id"], kind, path)
        return f"{self.public_base_url}/d/{token}.pdf"

    def _save_media(self, data: bytes, mime: str):
        ext = {"audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
               "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime.split(";")[0], ".bin")
        os.makedirs(self.media_dir, exist_ok=True)
        path = os.path.join(self.media_dir, uuid.uuid4().hex + ext)
        with open(path, "wb") as f:
            f.write(data)
        return path, ledger.sha256_bytes(data)


def days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


INTENT_TO_TOOL = {
    "log_work": "log_work", "record_payment": "record_payment", "ask_balance": "get_balance",
    "request_complaint": "make_complaint", "request_registration": "make_work_record",
    "report_death": "report_death",
}
TOOL_TO_INTENT = {tool: intent for intent, tool in INTENT_TO_TOOL.items()}

HOW_TO_LOG = (
    "Roz shaam ek voice note bhejiye: kahan kaam kiya, kiske saath, kitne paise.\n"
    "Every evening, send one voice note: where you worked, for whom, and your wage.\n"
    "Example: \"Worked with Ramesh today, 700 not paid yet\"\n\n"
    "Other things you can say:\n"
    "- \"Ramesh paid me 5000\"\n"
    "- \"How many days?\"\n"
    "- \"Ramesh has not paid me\" (get a complaint)\n"
    "- \"Registration\" (welfare board work record)"
)
