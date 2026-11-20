"""The conversation a worker has with KaamPramaan. Independent of WhatsApp so the
same logic serves WhatsApp, the web simulator and (later) a missed-call voice line."""
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date

from . import attest, ledger, muster, pdfs
from .ai import AI, BoardInfo, Understanding, _clean
from .db import Database
from .recommend import CachedEmbedder, recommend
from .records import WELFARE_DAYS_REQUIRED, live_entries, rupees, summarise, verify_days

# What the site QR poster pre-fills in WhatsApp: "HAZIRI K7M2QX". Typing it works too.
CHECK_IN_RE = re.compile(r"^\s*(?:haziri|hazri|hajiri|attendance|check[\s-]?in)\s*[:#-]?\s*([A-Za-z0-9]{6})\s*$", re.I)
CONFIRM_RE = re.compile(r"^\s*(?:confirm|pushti|sahi|ok|theek)\b(.*)$", re.I)


def _is_supervisor_command(text: str | None) -> bool:
    t = (text or "").strip().lower()
    return t.startswith("muster") or bool(CONFIRM_RE.match(t))

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
    footer: str = ""       # proof reference and undo hint, added to every reply that saved an entry


@dataclass
class Bot:
    db: Database
    ai: AI
    media_dir: str
    public_base_url: str
    today_fn: callable = field(default=date.today)
    embedder: CachedEmbedder | None = None   # None: offline hashing embedder

    def __post_init__(self):
        if self.embedder is None:
            self.embedder = CachedEmbedder(self.db)

    # ------------------------------------------------------------------ entry point
    def handle(self, msg: Inbound) -> list[Reply]:
        worker = self.db.get_worker_by_phone(msg.phone)

        supervised = self.db.get_site_by_supervisor(msg.phone)
        if supervised and (worker is None or msg.image or _is_supervisor_command(msg.text)):
            return self._on_supervisor(supervised, msg)

        code = CHECK_IN_RE.match(msg.text or "")
        if code:
            return self._on_check_in(worker, code.group(1).upper(), msg)

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
        site = self.db.get_site(worker["site_id"])
        if site:   # joined by scanning a site QR poster: the site is known, so mark today's attendance now
            self.db.update_worker(worker["id"], name=u.name, village=u.village, state="active")
            first = self._check_in(self.db.get_worker(worker["id"]), site, msg)
            return [Reply(f"Dhanyavaad, {u.name}. Welcome!\n\n{first.text}\n\n{HOW_TO_CHECK_IN}")]
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

    # ------------------------------------------------------------------ site QR check-in
    def _on_check_in(self, worker, code: str, msg) -> list[Reply]:
        site = self.db.get_site_by_code(code)
        if site is None:
            return [Reply(f"Site code {code} nahi mila. Site code {code} was not found. Please scan the QR poster "
                          "at your site again, or ask your supervisor for the code.")]
        if worker is None:
            self.db.create_worker(msg.phone, state="awaiting_name", site_id=site["id"])
            return [Reply(f"Namaste! Aap {site['project'] or 'is site'} par hain.\n"
                          f"Welcome to {site['project'] or 'this site'}. KaamPramaan keeps a record of your "
                          "working days for welfare board registration.\n\n"
                          "Apna naam aur gaon bhejiye. Please send your name and village. "
                          "Today's attendance is saved as soon as you do.")]
        if worker["state"] == "awaiting_name":
            self.db.update_worker(worker["id"], site_id=site["id"])
            return [Reply("Pehle apna naam aur gaon bhejiye. Please send your name and village first.")]
        return [self._check_in(worker, site, msg)]

    def _check_in(self, worker, site, msg) -> Reply:
        """One attendance entry per worker, site and day. The wage is not part of a check-in (amount 0);
        a worker can still log the wage by voice, which adds money but not a second day."""
        today = self.today_fn().isoformat()
        if worker["site_id"] != site["id"]:
            self.db.update_worker(worker["id"], site_id=site["id"])
        already = [e for e in live_entries(self.db.list_entries(worker["id"]))
                   if e["kind"] == "work" and e["work_date"] == today and e["site_id"] == site["id"]]
        place = site["project"] or "your site"
        if already:
            return Reply(f"Aaj ki haziri pehle se lagi hai. You are already marked present today at {place}.\n"
                         f"Ref: {already[0]['hash'][:12]}")
        entry = ledger.append(self.db, ledger.NewEntry(
            worker_id=worker["id"], kind="work", work_date=today, amount=0, source="site_qr", site_id=site["id"],
            contractor=site["contractor"], transcript=f"Checked in at {place} (site code {site['code']})",
            latitude=msg.latitude, longitude=msg.longitude))
        v = self._verification(worker["id"])
        return Reply(
            f"Haziri lag gayi / Present: {date.fromisoformat(today).strftime('%d %b')}, {place}.\n"
            f"Supervisor confirm karenge. Your supervisor will confirm it.\n"
            f"{_eligibility_line(v)}\n{_proof_footer(entry)}")

    # ------------------------------------------------------------------ site supervisor
    def _on_supervisor(self, site, msg) -> list[Reply]:
        """A supervisor confirms today's check-ins on WhatsApp, or sends a photo of the paper muster register."""
        if msg.image:
            import_id, draft = muster.read_photo(self.db, self.ai, site, msg.image, msg.image_mime, self.media_dir,
                                                 uploaded_by=f"supervisor {msg.phone}", today=self.today_fn())
            if not draft["rows"]:
                return [Reply("Register photo saved, lekin naam aur haziri padh nahi paaye. We could not read names "
                              f"and attendance from this photo (#{import_id}). Please send a clearer photo of the "
                              "whole page.")]
            days_total = sum(len(r["days"]) for r in draft["rows"])
            return [Reply(f"Register photo #{import_id} padh liya / read: {len(draft['rows'])} workers, "
                          f"{days_total} worker-days for {draft['month']:02d}/{draft['year']}.\n"
                          "An officer will review it on the website before it is added to the workers' records.")]
        today = self.today_fn().isoformat()
        rows = self.db.site_day(site["id"], today)
        place = site["project"] or "your site"
        m = CONFIRM_RE.match(msg.text or "")
        if m is None:
            if not rows:
                return [Reply(f"{place}: aaj abhi tak kisi ne haziri nahi lagayi. No check-ins yet today.\n"
                              f"Workers check in by scanning the QR poster, or by sending HAZIRI {site['code']}.\n"
                              "You can also send a photo of your muster register.")]
            lines = [f"{i}. {r['worker_name'] or 'Unnamed'}{' (confirmed)' if r['roles'] else ''}"
                     for i, r in enumerate(rows, 1)]
            return [Reply(f"{place}, {date.fromisoformat(today).strftime('%d %b')}: {len(rows)} checked in.\n"
                          + "\n".join(lines)
                          + "\n\nSab sahi hai to CONFIRM bhejiye. Send CONFIRM to confirm everyone, "
                            "or CONFIRM 1 3 to confirm only those numbers.")]
        if not rows:
            return [Reply("Aaj confirm karne ke liye koi haziri nahi hai. Nothing to confirm today.")]
        picked = {int(n) for n in re.findall(r"\d+", m.group(1))}
        chosen = [r for i, r in enumerate(rows, 1) if not picked or i in picked]
        if picked and len(chosen) != len(picked):
            return [Reply(f"Numbers 1 to {len(rows)} only. Send MUSTER to see the list again.")]
        who = f"{site['supervisor_name'] or 'Site supervisor'} ({msg.phone})"
        saved = attest.attest(self.db, chosen, "supervisor", who)
        names = ", ".join(r["worker_name"] or "Unnamed" for r in chosen)
        return [Reply(f"Confirmed {saved} of {len(chosen)} for {date.fromisoformat(today).strftime('%d %b')}"
                      f" ({names}). Dhanyavaad.")]

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
                intent=TOOL_TO_INTENT[name], transcript=str(args.get("transcript") or args.get("query") or ""),
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
            footer = "\n".join(o.footer for o in outcomes if o.footer)
            text = result.text + ("\n" + footer if footer else "")
            if docs:  # attach the PDF to Gemini's reply
                return [Reply(text, docs[-1].document_url, docs[-1].document_name)]
            return [Reply(text)]
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
        if name == "find_work":
            return self._find_work(worker, u)
        if name == "void_last_entry":
            return self._void_last(worker, msg, u)
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
        footer = _proof_footer(entry)
        return ToolOutcome(
            {"saved": True, "work_date": work_date, "wage": wage, "paid": bool(u.paid),
             "contractor": entry["contractor"], "days_this_month": s.days_this_month, "due": s.due},
            Reply(f"Saved: {d.strftime('%d %b')}, Rs. {rupees(wage)} ({'paid' if u.paid else 'not paid yet'}).\n"
                  f"Is mahine / This month: {days(s.days_this_month)}. Baaki / Due: Rs. {rupees(s.due)}.\n{footer}"),
            footer=footer)

    def _record_payment(self, worker, msg, media, u: Understanding) -> ToolOutcome:
        if not u.amount:
            return ToolOutcome(
                {"saved": False, "error": "amount missing, ask how much was received"},
                Reply("Kitne paise mile? How much were you paid? e.g. \"Ramesh paid me 5000\"."))
        entry = ledger.append(self.db, ledger.NewEntry(
            worker_id=worker["id"], kind="payment", work_date=self.today_fn().isoformat(), amount=u.amount,
            source=msg.source, site_id=worker["site_id"], contractor=u.contractor,
            transcript=u.transcript or msg.text, media_path=media[0], media_sha256=media[1],
        ))
        s = self._summary(worker["id"])
        footer = _proof_footer(entry)
        return ToolOutcome(
            {"saved": True, "amount": u.amount, "still_due": s.due},
            Reply(f"Payment noted: Rs. {rupees(u.amount)}. Baaki / Still due: Rs. {rupees(s.due)}.\n{footer}"),
            footer=footer)

    def _void_last(self, worker, msg, u: Understanding) -> ToolOutcome:
        res = ledger.void_last(self.db, worker["id"], source=msg.source, transcript=u.transcript or msg.text)
        if res is None:
            return ToolOutcome(
                {"cancelled": False, "reason": "nothing from the last 24 hours to cancel"},
                Reply("Cancel nahi ho saka. Sirf pichle 24 ghante ki entry cancel hoti hai. "
                      "Nothing from the last 24 hours to cancel. An NGO volunteer can help with older entries."))
        target, void = res
        # the default wage for the next day should come from an entry that still counts
        works = [e for e in live_entries(self.db.list_entries(worker["id"])) if e["kind"] == "work" and e["amount"]]
        self.db.update_worker(worker["id"], last_wage=works[-1]["amount"] if works else None)
        s = self._summary(worker["id"])
        day = date.fromisoformat(target["work_date"]).strftime("%d %b")
        if target["kind"] == "payment":
            what = f"payment of Rs. {rupees(target['amount'])}"
        elif target["amount"]:
            what = f"{day} work, Rs. {rupees(target['amount'])}"
        else:
            what = f"{day} attendance"
        return ToolOutcome(
            {"cancelled": True, "entry": what, "amount": target["amount"], "due": s.due,
             "days_this_month": s.days_this_month},
            Reply(f"Cancelled: {what}. Ref: {void['hash'][:12]}.\n"
                  f"Ab sahi entry bhejiye. Send the correct entry now. Baaki / Due: Rs. {rupees(s.due)}."),
            footer=f"Ref: {void['hash'][:12]}")

    def _balance(self, worker) -> ToolOutcome:
        s = self._summary(worker["id"])
        v = self._verification(worker["id"])
        return ToolOutcome(
            {"days_worked": s.days_worked, "days_last_12_months": s.days_last_12_months,
             "days_confirmed_by_employer_last_12_months": v.employer_days_12m,
             "days_this_month": s.days_this_month, "earned": s.earned, "received": s.paid, "due": s.due,
             "ready_for_welfare_board": v.ready_to_register, "confirmed_days_still_needed": v.verified_days_to_go},
            Reply(f"Total days worked: {s.days_worked}. Last 12 months: {s.days_last_12_months}.\n"
                  f"Earned Rs. {rupees(s.earned)}, received Rs. {rupees(s.paid)}, due Rs. {rupees(s.due)}.\n"
                  f"{_eligibility_line(v)}"))

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

    def _find_work(self, worker, u: Understanding) -> ToolOutcome:
        query = (u.transcript or "").strip() or None
        recs = recommend(self.db, worker, query, self.embedder, self.today_fn())
        if not recs:
            return ToolOutcome({"openings": [], "reason": "no open work right now"},
                               Reply("Abhi koi kaam nahi mila. No openings right now. Hum naya kaam aate hi batayenge."))
        data, lines = [], []
        for n, r in enumerate(recs, 1):
            o = r.opening
            data.append({"rank": n, "title": o["title"], "wage": o["wage"], "place": o["location"],
                         "distance_km": r.distance_km, "contractor": o["contractor"], "contact": o["contact"],
                         "starts_on": o["starts_on"], "why": r.reasons, "warning": r.warning})
            line = f"{n}. {o['title']}, Rs. {rupees(o['wage'])}/day"
            line += f", {o['location']}" if o["location"] else ""
            line += f" ({r.distance_km} km)" if r.distance_km is not None else ""
            line += f". Call {o['contact']}" if o["contact"] else ""
            if r.warning:
                line += f"\n   Dhyan dein / Careful: {r.warning}."
            lines.append(line)
        return ToolOutcome({"openings": data},
                           Reply("Aapke liye kaam / Work for you:\n" + "\n".join(lines)))

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
        # tracked from here on: an NGO marks it filed and records what was recovered (website, /dashboard)
        self.db.open_wage_case(worker["id"], s.due, f"{len(s.unpaid_days)} unpaid days, "
                               f"{facts['from_date']} to {facts['to_date']}, {facts['contractor']}")
        return ToolOutcome(
            {"created": True, "due": s.due, "unpaid_days": len(s.unpaid_days),
             "from_date": facts["from_date"], "to_date": facts["to_date"], "pdf_attached": True,
             "next_step": "show the complaint to the labour office or an NGO"},
            Reply(f"Aapke Rs. {rupees(s.due)} baaki hain ({len(s.unpaid_days)} din). "
                  f"You are owed Rs. {rupees(s.due)} for {days(len(s.unpaid_days))}.\n"
                  "Yeh shikayat labour office ya NGO ko dikhaiye. Show this complaint to the labour office or an NGO.",
                  document_url=url, document_name="KaamPramaan_complaint.pdf"))

    def _registration(self, worker) -> ToolOutcome:
        """The pre-filled welfare board application with the 90-day work certificate: each working day says who
        vouched for it (employer side, co-workers, or only the worker)."""
        entries = self.db.list_entries(worker["id"])
        s = summarise(entries, today=self.today_fn())
        v = self._verification(worker["id"], entries)
        site = self.db.get_site(worker["site_id"])
        chain = ledger.verify_chain(self.db, worker["id"])
        att = attest.verify_attestations(self.db, worker["id"])
        sites = {e["site_id"]: self.db.get_site(e["site_id"]) for e in entries if e["site_id"]}
        url = self._make_doc(worker, "application", lambda path: pdfs.application_pdf(
            path, worker, site, sites, entries, s, v, chain, att, today=self.today_fn()))
        self.db.update_worker(worker["id"], application_at=self.today_fn().isoformat())
        tail = (" Yeh welfare board office mein jama kijiye. Take this to the welfare board office or facilitation "
                "centre." if v.ready_to_register else "")
        return ToolOutcome(
            {"created": True, "days_last_12_months": s.days_last_12_months,
             "days_confirmed_by_employer_last_12_months": v.employer_days_12m, "days_required": WELFARE_DAYS_REQUIRED,
             "ready_for_welfare_board": v.ready_to_register, "confirmed_days_still_needed": v.verified_days_to_go,
             "pdf_attached": True},
            Reply(_eligibility_line(v, ask=False) + tail, document_url=url, document_name="KaamPramaan_application.pdf"))

    # ------------------------------------------------------------------ helpers
    def _summary(self, worker_id):
        return summarise(self.db.list_entries(worker_id), today=self.today_fn())

    def _verification(self, worker_id, entries=None):
        entries = entries if entries is not None else self.db.list_entries(worker_id)
        return verify_days(entries, self.db.attestations_for(worker_id), self.db.corroboration_counts(worker_id),
                           today=self.today_fn())

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


def _proof_footer(entry: dict) -> str:
    """The reference matches the one printed in the PDFs, and the WhatsApp message itself carries a
    timestamp from Meta, so the worker holds proof that does not depend on our database."""
    return (f"Ref: {entry['hash'][:12]}. Galat hai? \"galat\" bhejiye. If wrong, send \"wrong\".")


def _eligibility_line(v, ask: bool = True) -> str:
    if v.ready_to_register:
        return (f"{v.employer_days_12m} din confirm hain: welfare board registration ke liye taiyaar. "
                f"{v.employer_days_12m} days confirmed by your employer in the last 12 months: ready for welfare "
                "board registration." + (" Send \"registration\" for your application." if ask else ""))
    return (f"Pichle 12 mahine: {v.total_days_12m} din, {v.employer_days_12m} confirm. "
            f"Last 12 months: {v.total_days_12m} days, {v.employer_days_12m} confirmed by your employer. "
            f"{v.verified_days_to_go} more confirmed days needed for welfare board registration.")


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
    "report_death": "report_death", "find_work": "find_work", "cancel_last": "void_last_entry",
}
TOOL_TO_INTENT = {tool: intent for intent, tool in INTENT_TO_TOOL.items()}

HOW_TO_CHECK_IN = (
    "Roz site par QR poster scan kijiye aur Send dabaiye. Every day at the site, scan the QR poster and press Send. "
    "Your supervisor confirms it, and after 90 confirmed days you get your welfare board application."
)

HOW_TO_LOG = (
    "Roz site par QR poster scan kijiye (ya HAZIRI aur site code bhejiye).\n"
    "Every day at the site, scan the QR poster (or send HAZIRI and the site code).\n"
    "Wage bhi likhna ho to voice note bhejiye: \"Worked with Ramesh today, 700 not paid yet\"\n\n"
    "Other things you can say:\n"
    "- \"Ramesh paid me 5000\"\n"
    "- \"How many days?\"\n"
    "- \"Ramesh has not paid me\" (get a complaint)\n"
    "- \"Registration\" (welfare board application)\n"
    "- \"Kaam chahiye\" (find new work)\n"
    "- \"Galat\" / \"wrong\" (cancel your last entry)"
)
