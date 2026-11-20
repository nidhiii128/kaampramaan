"""FastAPI app: WhatsApp webhook, welfare board and NGO website, site QR posters, chat simulator, public demo."""
import calendar
import json
import logging
import os
import secrets
from datetime import date, timedelta

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import attest, board, ledger, muster, qr
from . import demo as demo_tools
from .ai import AI
from .chat import Bot, Inbound
from .config import Settings, load_settings
from .db import WAGE_CASE_STATUSES, Database
from .recommend import CachedEmbedder, GeminiEmbedder
from .records import LEVEL_TEXT, live_entries, rupees, summarise, verify_days
from .voice import speakable, wav_to_ogg
from .whatsapp import WhatsAppClient, parse_webhook, verify_signature

log = logging.getLogger("kaampramaan")
HERE = os.path.dirname(__file__)


def create_app(settings: Settings | None = None, ai: AI | None = None, today_fn=None) -> FastAPI:
    s = settings or load_settings()
    today_fn = today_fn or s.today
    db = Database(s.db_path)
    ai = ai or AI(s.gemini_api_key, s.gemini_model, s.gemini_fallback_models, s.gemini_tts_models,
                  s.gemini_tts_voice)
    embedder = CachedEmbedder(db, GeminiEmbedder(ai.client, s.gemini_embed_model) if ai.client else None)
    bot = Bot(db=db, ai=ai, media_dir=s.media_dir, public_base_url=s.public_base_url, today_fn=today_fn,
              embedder=embedder)
    wa = WhatsAppClient(s)

    app = FastAPI(title="KaamPramaan", docs_url=None, redoc_url=None)
    app.state.db, app.state.bot = db, bot
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    tpl = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    tpl.env.filters["rupees"] = rupees
    security = HTTPBasic()

    def staff(creds: HTTPBasicCredentials = Depends(security)) -> str:
        if not s.admin_password:
            raise HTTPException(503, "Set KP_ADMIN_PASSWORD to use the website")
        ok = secrets.compare_digest(creds.username, s.admin_user) and \
            secrets.compare_digest(creds.password, s.admin_password)
        if not ok:
            raise HTTPException(401, "Wrong username or password", headers={"WWW-Authenticate": "Basic"})
        return creds.username

    # ------------------------------------------------------------------ health
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "gemini": ai.enabled, "whatsapp": wa.configured}

    # ------------------------------------------------------------------ WhatsApp webhook
    @app.get("/webhook")
    def verify_webhook(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and s.wa_verify_token and \
                secrets.compare_digest(q.get("hub.verify_token", ""), s.wa_verify_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @app.post("/webhook")
    async def receive_webhook(request: Request, background: BackgroundTasks):
        body = await request.body()
        if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), s.wa_app_secret):
            raise HTTPException(401, "bad signature")
        for m in parse_webhook(await request.json()):
            if m.message_id and db.mark_processed(m.message_id):
                background.add_task(process_whatsapp_message, m)
        return {"ok": True}  # answer fast; Meta retries slow webhooks

    def process_whatsapp_message(m):
        try:
            msg = Inbound(phone=m.phone, text=m.text, latitude=m.latitude, longitude=m.longitude)
            if m.media_id:
                data, mime = wa.download_media(m.media_id)
                if m.type == "audio":
                    msg.audio, msg.audio_mime = data, mime.split(";")[0]
                else:
                    msg.image, msg.image_mime = data, mime
            replies = bot.handle(msg)
            for r in replies:
                if r.document_url:
                    wa.send_document(m.phone, r.document_url, r.document_name or "record.pdf", r.text)
                else:
                    wa.send_text(m.phone, r.text)
            if s.voice_replies and m.type == "audio" and replies:
                send_voice_reply(m.phone, replies[0].text)   # a worker who speaks gets spoken answers
        except Exception:
            log.exception("failed to process WhatsApp message %s", m.message_id)
            wa.send_text(m.phone, "Sorry, kuch galat ho gaya. Something went wrong, please try again.")

    def send_voice_reply(phone: str, text: str):
        """Gemini TTS -> WAV -> ogg/opus -> WhatsApp. Any failure just means the worker keeps the text reply."""
        try:
            wav = ai.speak(speakable(text))
            ogg = wav_to_ogg(wav) if wav else None
            if ogg:
                wa.send_audio(phone, ogg)
            else:
                log.warning("voice reply skipped (no audio from Gemini, or ffmpeg missing)")
        except Exception:
            log.exception("voice reply failed")

    # ------------------------------------------------------------------ documents (link sent on WhatsApp)
    @app.get("/d/{token}.pdf")
    def document(token: str):
        doc = db.get_document(token)
        if not doc or not os.path.exists(doc["path"]):
            raise HTTPException(404)
        return FileResponse(doc["path"], media_type="application/pdf",
                            filename=f"KaamPramaan_{doc['kind']}.pdf")

    # ------------------------------------------------------------------ website for NGOs, volunteers, families
    @app.get("/")
    def home():
        return RedirectResponse("/board")

    # ------------------------------------------------------------------ welfare board view
    @app.get("/board")
    def board_page(request: Request, user: str = Depends(staff), sent: int | None = None):
        data = board.overview(db, today_fn())
        return tpl.TemplateResponse(request, "board.html", {**data, "today": today_fn(), "sent": sent,
                                                            "required": 90, "renewal_days": board.RENEWAL_DAYS})

    @app.post("/board/reminders")
    def board_reminders(user: str = Depends(staff)):
        """WhatsApp every registered worker whose renewal is due within the warning window."""
        sent = 0
        for r in board.overview(db, today_fn())["renewals"]:
            if r.w["phone"]:
                when = r.renewal_on.strftime("%d %b %Y")
                wa.send_text(r.w["phone"], f"Aapka welfare board registration {when} ko renew hona hai. Your welfare "
                                           f"board registration is due for renewal on {when}. Send \"registration\" "
                                           "for an updated work certificate.")
                sent += 1
        return RedirectResponse(f"/board?sent={sent}", status_code=303)

    @app.post("/workers/{worker_id}/registered")
    def worker_registered(worker_id: int, reg_no: str = Form(...), reg_date: str = Form(...),
                          user: str = Depends(staff)):
        db.get_worker(worker_id) or _404()
        try:
            d = date.fromisoformat(reg_date)
        except ValueError:
            raise HTTPException(400, "bad date")
        if d > today_fn() or not reg_no.strip() or len(reg_no) > 64:
            raise HTTPException(400, "registration number is required and the date cannot be in the future")
        db.update_worker(worker_id, board_reg_no=reg_no.strip(), board_reg_date=d.isoformat())
        return RedirectResponse(f"/workers/{worker_id}", status_code=303)

    # ------------------------------------------------------------------ sites: QR posters, muster, supervisors
    @app.get("/sites")
    def sites_page(request: Request, user: str = Depends(staff)):
        return tpl.TemplateResponse(request, "sites.html", {"sites": db.list_sites(today_fn().isoformat()),
                                                            "imports": db.list_muster_imports()})

    @app.post("/sites")
    def sites_create(project: str = Form(...), principal_employer: str = Form(""), employer_type: str = Form("unknown"),
                     contractor: str = Form(""), location: str = Form(""), supervisor_name: str = Form(""),
                     supervisor_phone: str = Form(""), user: str = Depends(staff)):
        if employer_type not in ("government", "developer", "small", "unknown") or not project.strip():
            raise HTTPException(400, "project name is required")
        sid = db.create_site(project=project.strip(), principal_employer=principal_employer.strip() or None,
                             employer_type=employer_type, contractor=contractor.strip().title() or None,
                             location=location.strip() or None, supervisor_name=supervisor_name.strip() or None,
                             supervisor_phone=_phone(supervisor_phone))
        return RedirectResponse(f"/sites/{sid}", status_code=303)

    @app.get("/sites/{site_id}")
    def site_page(site_id: int, request: Request, day: str = "", user: str = Depends(staff)):
        site = db.get_site(site_id) or _404()
        try:
            d = date.fromisoformat(day) if day else today_fn()
        except ValueError:
            raise HTTPException(400, "bad date")
        return tpl.TemplateResponse(request, "site.html", {
            "site": site, "day": d, "today": today_fn(), "rows": db.site_day(site_id, d.isoformat()),
            "prev": (d - timedelta(days=1)).isoformat(), "next": (d + timedelta(days=1)).isoformat(),
            "imports": db.list_muster_imports(site_id), "ai": ai.enabled,
            "check_in_text": qr.check_in_text(site["code"])})

    @app.post("/sites/{site_id}/supervisor")
    def site_supervisor(site_id: int, supervisor_name: str = Form(""), supervisor_phone: str = Form(""),
                        user: str = Depends(staff)):
        db.get_site(site_id) or _404()
        db.set_supervisor(site_id, supervisor_name.strip(), _phone(supervisor_phone))
        return RedirectResponse(f"/sites/{site_id}", status_code=303)

    @app.post("/sites/{site_id}/confirm")
    async def site_confirm(site_id: int, request: Request, user: str = Depends(staff)):
        """An officer confirms check-ins on the website (for supervisors who do not use WhatsApp)."""
        db.get_site(site_id) or _404()
        form = await request.form()
        day = str(form.get("day", ""))
        ids = {int(x) for x in form.getlist("entry_id") if str(x).isdigit()}
        rows = [r for r in db.site_day(site_id, day) if r["id"] in ids]
        if rows:
            attest.attest(db, rows, "officer", f"{user} (website)")
        return RedirectResponse(f"/sites/{site_id}?day={day}", status_code=303)

    @app.get("/sites/{site_id}/poster")
    def site_poster(site_id: int, request: Request, user: str = Depends(staff)):
        site = db.get_site(site_id) or _404()
        link = qr.check_in_link(site["code"], s.wa_number)
        return tpl.TemplateResponse(request, "poster.html", {
            "site": site, "qr_svg": qr.svg(link), "link": link, "wa_number": s.wa_number,
            "check_in_text": qr.check_in_text(site["code"])})

    @app.post("/sites/{site_id}/muster")
    async def site_muster_upload(site_id: int, file: UploadFile = File(...), user: str = Depends(staff)):
        site = db.get_site(site_id) or _404()
        data = await file.read(16 * 1024 * 1024 + 1)
        mime = (file.content_type or "").split(";")[0]
        if len(data) > 16 * 1024 * 1024 or not mime.startswith("image/"):
            raise HTTPException(400, "send a photo (JPEG or PNG) under 16 MB")
        import_id, _ = muster.read_photo(db, ai, site, data, mime, s.media_dir, uploaded_by=user, today=today_fn())
        return RedirectResponse(f"/muster/{import_id}", status_code=303)

    @app.get("/muster/{import_id}")
    def muster_review(import_id: int, request: Request, user: str = Depends(staff)):
        imp = db.get_muster_import(import_id) or _404()
        return tpl.TemplateResponse(request, "muster.html", {
            "imp": imp, "site": db.get_site(imp["site_id"]), "draft": json.loads(imp["extracted"]), "ai": ai.enabled})

    @app.get("/muster/{import_id}/photo")
    def muster_photo(import_id: int, user: str = Depends(staff)):
        imp = db.get_muster_import(import_id) or _404()
        if not os.path.exists(imp["photo_path"]):
            _404()
        return FileResponse(imp["photo_path"])

    @app.post("/muster/{import_id}")
    async def muster_import(import_id: int, request: Request, user: str = Depends(staff)):
        """Save the reviewed rows. The reviewer can fix names and days, untick rows, and add rows Gemini missed."""
        imp = db.get_muster_import(import_id) or _404()
        if imp["status"] != "draft":
            raise HTTPException(409, f"this register photo was already {imp['status']}")
        form = await request.form()
        try:
            month, year = int(form.get("month", 0)), int(form.get("year", 0))
            date(year, month, 1)
        except (TypeError, ValueError):
            raise HTTPException(400, "bad month or year")
        if date(year, month, 1) > today_fn():
            raise HTTPException(400, "the register month cannot be in the future")
        last_day = calendar.monthrange(year, month)[1]
        rows = []
        for i in range(int(form.get("count", 0) or 0) + 1):          # +1: the empty row for something Gemini missed
            name = str(form.get(f"name_{i}", "")).strip()
            if name and form.get(f"keep_{i}") and len(name) <= 80:
                rows.append({"name": name, "days": muster.parse_days(str(form.get(f"days_{i}", "")), last_day)})
        if not rows:
            raise HTTPException(400, "tick at least one row to import, or reject the photo")
        muster.import_rows(db, imp, rows, month, year, reviewer=user, today=today_fn())
        return RedirectResponse(f"/sites/{imp['site_id']}", status_code=303)

    @app.post("/muster/{import_id}/reject")
    def muster_reject(import_id: int, user: str = Depends(staff)):
        imp = db.get_muster_import(import_id) or _404()
        if imp["status"] == "draft":
            db.close_muster_import(import_id, "rejected", user)
        return RedirectResponse(f"/sites/{imp['site_id']}", status_code=303)

    @app.get("/dashboard")
    def dashboard(request: Request, user: str = Depends(staff)):
        rows = board.worker_rows(db, today_fn())
        totals = {
            "workers": len(rows),
            "due": sum(r.s.due for r in rows),
            "eligible": sum(1 for r in rows if r.v.ready_to_register),
            "days": sum(r.s.days_worked for r in rows),
        }
        return tpl.TemplateResponse(request, "dashboard.html", {
            "rows": rows, "totals": totals, "cases": db.list_cases(), "ai": ai.enabled,
            "impact": db.impact_stats(), "wage_cases": db.list_wage_cases(), "statuses": WAGE_CASE_STATUSES})

    @app.get("/workers/{worker_id}")
    def worker_page(worker_id: int, request: Request, user: str = Depends(staff)):
        w = db.get_worker(worker_id) or _404()
        entries = db.list_entries(worker_id)
        corr, roles = db.corroboration_counts(worker_id), db.attestations_for(worker_id)
        return tpl.TemplateResponse(request, "worker.html", {
            "w": w, "site": db.get_site(w["site_id"]), "entries": entries,
            "s": summarise(entries, today=today_fn()),
            "v": verify_days(entries, roles, corr, today=today_fn()), "roles": roles, "level_text": LEVEL_TEXT,
            "chain": ledger.verify_chain(db, worker_id, media_reader=_read_file),
            "att": attest.verify_attestations(db, worker_id),
            "corr": corr, "today": today_fn().isoformat(),
        })

    @app.post("/workers/{worker_id}/complaint")
    def worker_complaint(worker_id: int, user: str = Depends(staff)):
        w = db.get_worker(worker_id) or _404()
        return _doc_redirect(bot._complaint(w))

    @app.post("/workers/{worker_id}/work-record")
    def worker_record(worker_id: int, user: str = Depends(staff)):
        w = db.get_worker(worker_id) or _404()
        return _doc_redirect(bot._registration(w))

    @app.post("/sites/{site_id}")
    def update_site(site_id: int, worker_id: int = Form(...), project: str = Form(""),
                    principal_employer: str = Form(""), employer_type: str = Form("unknown"),
                    contractor: str = Form(""), user: str = Depends(staff)):
        if employer_type not in ("government", "developer", "small", "unknown"):
            raise HTTPException(400, "bad employer type")
        with db.connect() as c:
            c.execute("""UPDATE sites SET project=?, principal_employer=?, employer_type=?, contractor=?
                         WHERE id=?""", (project or None, principal_employer or None, employer_type,
                                         contractor or None, site_id))
        return RedirectResponse(f"/workers/{worker_id}", status_code=303)

    @app.get("/volunteer")
    def volunteer_form(request: Request, user: str = Depends(staff)):
        with db.connect() as c:
            sites = c.execute("SELECT * FROM sites ORDER BY created_at DESC").fetchall()
        return tpl.TemplateResponse(request, "volunteer.html",
                                    {"sites": sites, "today": today_fn().isoformat(), "done": None})

    @app.post("/volunteer")
    def volunteer_submit(request: Request, workers: str = Form(...), site_id: str = Form(""),
                         new_site: str = Form(""), work_date: str = Form(...), wage: int = Form(...),
                         contractor: str = Form(""), paid: bool = Form(False), user: str = Depends(staff)):
        """Log one day of work for a group of workers who have no phone."""
        try:
            d = date.fromisoformat(work_date)
        except ValueError:
            raise HTTPException(400, "bad date")
        if d > today_fn() or not (0 < wage <= 100000):
            raise HTTPException(400, "date cannot be in the future and wage must be positive")
        sid = int(site_id) if site_id else (db.create_site(project=new_site.strip()) if new_site.strip() else None)
        logged = []
        for line in workers.splitlines():
            if not line.strip():
                continue
            name, _, village = (p.strip() for p in line.partition(","))
            with db.connect() as c:
                row = c.execute("SELECT id FROM workers WHERE phone IS NULL AND lower(name)=lower(?) "
                                "AND lower(coalesce(village,''))=lower(?)", (name, village)).fetchone()
            wid = row["id"] if row else db.create_worker(None, name=name.title(), village=village.title() or None,
                                                         state="active", site_id=sid)
            ledger.append(db, ledger.NewEntry(
                worker_id=wid, kind="work", work_date=d.isoformat(), amount=wage, source="volunteer",
                site_id=sid, contractor=contractor.strip().title() or None, paid=paid,
                transcript=f"Logged by volunteer ({user})",
            ))
            logged.append(name.title())
        with db.connect() as c:
            sites = c.execute("SELECT * FROM sites ORDER BY created_at DESC").fetchall()
        return tpl.TemplateResponse(request, "volunteer.html",
                                    {"sites": sites, "today": today_fn().isoformat(), "done": logged})

    # ------------------------------------------------------------------ work openings
    @app.get("/openings")
    def openings_page(request: Request, user: str = Depends(staff)):
        return tpl.TemplateResponse(request, "openings.html", {
            "openings": db.list_openings(), "today": today_fn().isoformat(), "saved": None})

    @app.post("/openings")
    def openings_create(request: Request, title: str = Form(...), wage: int = Form(...),
                        starts_on: str = Form(...), ends_on: str = Form(""), details: str = Form(""),
                        contractor: str = Form(""), location: str = Form(""), latitude: str = Form(""),
                        longitude: str = Form(""), slots: int = Form(1), contact: str = Form(""),
                        user: str = Depends(staff)):
        try:
            start = date.fromisoformat(starts_on)
            end = date.fromisoformat(ends_on) if ends_on else None
            lat = float(latitude) if latitude.strip() else None
            lon = float(longitude) if longitude.strip() else None
        except ValueError:
            raise HTTPException(400, "bad date or location")
        if not title.strip() or not (0 < wage <= 100000) or not (0 < slots <= 1000):
            raise HTTPException(400, "title, wage and slots are required")
        if end and end < start:
            raise HTTPException(400, "end date is before start date")
        if (lat is None) != (lon is None) or (lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180)):
            raise HTTPException(400, "give both latitude and longitude, or neither")
        db.create_opening(title=title.strip(), wage=wage, starts_on=start.isoformat(),
                          ends_on=end.isoformat() if end else None, details=details.strip() or None,
                          contractor=contractor.strip().title() or None, location=location.strip() or None,
                          latitude=lat, longitude=lon, slots=slots, contact=contact.strip() or None, posted_by=user)
        return tpl.TemplateResponse(request, "openings.html", {
            "openings": db.list_openings(), "today": today_fn().isoformat(), "saved": title.strip()})

    @app.post("/openings/{opening_id}/close")
    def openings_close(opening_id: int, user: str = Depends(staff)):
        db.close_opening(opening_id)
        return RedirectResponse("/openings", status_code=303)

    # ------------------------------------------------------------------ wage complaint follow-up
    @app.post("/cases/{case_id}")
    def update_case(case_id: int, status: str = Form(...), amount_recovered: int = Form(0),
                    user: str = Depends(staff)):
        """An NGO marks a complaint filed / resolved and records the money recovered. The recovered amount is
        added to the worker's ledger as a payment, so their balance and every PDF stay in step."""
        case = db.get_case(case_id)
        if not case or case["kind"] != "wage_complaint":
            _404()
        if status not in WAGE_CASE_STATUSES:
            raise HTTPException(400, "bad status")
        before = case["amount_recovered"] or 0
        if not (before <= amount_recovered <= 10_000_000):
            raise HTTPException(400, "recovered amount can only go up (the ledger is append-only)")
        delta = amount_recovered - before
        db.update_case(case_id, status, amount_recovered)
        if delta:
            try:
                ledger.append(db, ledger.NewEntry(
                    worker_id=case["worker_id"], kind="payment", work_date=today_fn().isoformat(), amount=delta,
                    source="ngo", transcript=f"Recovered through complaint case #{case_id} (recorded by {user})"))
            except Exception:
                db.update_case(case_id, case["status"], before)   # keep case and ledger in step
                raise
            worker = db.get_worker(case["worker_id"])
            if worker and worker["phone"]:
                wa.send_text(worker["phone"], f"Aapke Rs. {rupees(delta)} mile aur record mein jod diye gaye "
                                              f"(case #{case_id}). Rs. {rupees(delta)} recovered and added to your record.")
        return RedirectResponse("/dashboard#cases", status_code=303)

    # ------------------------------------------------------------------ public demo sandbox (KP_DEMO_MODE=1)
    if s.demo_mode:
        send_limit = demo_tools.RateLimiter(30, 600)
        speak_limit = demo_tools.RateLimiter(12, 600)
        tampered: dict[str, tuple[int, int]] = {}          # visitor -> (entry id, original amount)

        def visitor_id(request: Request) -> str:
            vid = request.cookies.get(demo_tools.COOKIE, "")
            if not demo_tools.VISITOR_RE.match(vid):
                raise HTTPException(400, "Open /demo first")
            return vid

        def throttle(request: Request, limiter):
            ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                  or (request.client.host if request.client else "?"))
            if not limiter.allow(ip):
                raise HTTPException(429, "Too many requests. Please wait a few minutes.")

        def demo_worker(request: Request):
            return demo_tools.ensure_worker(db, demo_tools.demo_phone(visitor_id(request)))

        @app.get("/demo")
        def demo_page(request: Request):
            vid = request.cookies.get(demo_tools.COOKIE, "")
            if not demo_tools.VISITOR_RE.match(vid):
                vid = demo_tools.new_visitor_id()
            db.purge_stale_demo(24)
            worker = demo_tools.ensure_worker(db, demo_tools.demo_phone(vid))
            site = db.get_site(worker["site_id"])
            resp = tpl.TemplateResponse(request, "demo.html", {
                "ai": ai.client is not None, "check_in_text": qr.check_in_text(site["code"]),
                "qr_svg": qr.svg(qr.check_in_link(site["code"], s.wa_number))})
            resp.set_cookie(demo_tools.COOKIE, vid, max_age=6 * 3600, httponly=True, samesite="lax",
                            secure=s.public_base_url.startswith("https"))
            return resp

        @app.post("/demo/send")
        async def demo_send(request: Request, text: str = Form(""), file: UploadFile | None = File(None)):
            worker = demo_worker(request)
            throttle(request, send_limit)
            msg = Inbound(phone=worker["phone"], text=text.strip()[:1000] or None, source="demo")
            if file is not None and file.filename:
                data = await file.read(demo_tools.MAX_UPLOAD + 1)
                if len(data) > demo_tools.MAX_UPLOAD:
                    raise HTTPException(413, "file too large")
                mime = (file.content_type or "").split(";")[0]
                if mime.startswith("audio/"):
                    msg.audio, msg.audio_mime = data, mime
                elif mime.startswith("image/"):
                    msg.image, msg.image_mime = data, mime
            replies = bot.handle(msg)
            return JSONResponse([{"text": r.text, "document_url": (r.document_url or "").replace(s.public_base_url, "") or None}
                                 for r in replies])

        @app.get("/demo/ledger")
        def demo_ledger(request: Request):
            worker = demo_worker(request)
            entries = db.list_entries(worker["id"])
            sm = summarise(entries, today=today_fn())
            roles = db.attestations_for(worker["id"])
            v = verify_days(entries, roles, db.corroboration_counts(worker["id"]), today=today_fn())
            chain = ledger.verify_chain(db, worker["id"], media_reader=_read_file)
            return {
                "chain": {"ok": chain.ok, "entries_checked": chain.entries_checked,
                          "broken_at_seq": chain.broken_at_seq, "reason": chain.reason},
                "summary": {"days": sm.days_worked, "earned": sm.earned, "received": sm.paid, "due": sm.due,
                            "cancelled": sm.cancelled_entries, "confirmed": v.employer_days_12m,
                            "required": 90, "ready": v.ready_to_register},
                "entries": [{"seq": e["seq"], "kind": e["kind"], "date": e["work_date"], "amount": e["amount"],
                             "paid": bool(e["paid"]), "contractor": e["contractor"], "ref": e["hash"][:12],
                             "said": e["transcript"], "void_of": e["void_of"], "source": e["source"],
                             "confirmed_by": sorted(roles.get(e["id"], ()))}
                            for e in sorted(entries, key=lambda e: (e["work_date"], e["seq"]))][-40:],
                "tampered": visitor_id(request) in tampered,
            }

        @app.post("/demo/supervisor")
        def demo_supervisor(request: Request, text: str = Form(...)):
            """The sandbox site's supervisor, on their own WhatsApp: MUSTER lists today's check-ins, CONFIRM vouches."""
            worker = demo_worker(request)
            throttle(request, send_limit)
            demo_tools.ensure_worker(db, worker["phone"])          # older sandboxes get a supervisor here
            replies = bot.handle(Inbound(phone=demo_tools.supervisor_phone(worker["phone"]), text=text.strip()[:200],
                                         source="demo"))
            return JSONResponse([{"text": r.text} for r in replies])

        @app.post("/demo/history")
        def demo_history(request: Request):
            """Fast-forward 89 supervisor-confirmed days so the 90-day rule can be reached in the demo."""
            worker = demo_worker(request)
            throttle(request, send_limit)
            if any(e["work_date"] < today_fn().isoformat() for e in db.list_entries(worker["id"]) if e["kind"] == "work"):
                raise HTTPException(409, "This sandbox already has past days. Press Start over to try again.")
            return {"added": demo_tools.add_confirmed_history(db, worker, today_fn())}

        @app.post("/demo/tamper")
        def demo_tamper(request: Request):
            """Edit a past entry behind the ledger's back, the way a dishonest operator would, so the record
            check can be seen catching it. Only ever touches this visitor's sandbox worker."""
            worker = demo_worker(request)
            vid = visitor_id(request)
            if vid in tampered:
                raise HTTPException(409, "Already tampered. Restore the entry or reset the demo.")
            work = [e for e in live_entries(db.list_entries(worker["id"])) if e["kind"] == "work"]
            if not work:
                raise HTTPException(409, "Log a day of work first, then try to change it.")
            target, new_amount = work[0], max(1, work[0]["amount"] // 10)
            with db.connect() as c:
                c.execute("UPDATE entries SET amount = ? WHERE id = ? AND worker_id = ?",
                          (new_amount, target["id"], worker["id"]))
            tampered[vid] = (target["id"], target["amount"])
            return {"seq": target["seq"], "was": target["amount"], "now": new_amount}

        @app.post("/demo/restore")
        def demo_restore(request: Request):
            worker = demo_worker(request)
            vid = visitor_id(request)
            if vid not in tampered:
                raise HTTPException(409, "Nothing to restore")
            entry_id, original = tampered.pop(vid)
            with db.connect() as c:
                c.execute("UPDATE entries SET amount = ? WHERE id = ? AND worker_id = ?",
                          (original, entry_id, worker["id"]))
            return {"restored": original}

        @app.post("/demo/reset")
        def demo_reset(request: Request):
            vid = visitor_id(request)
            db.purge_demo_worker(demo_tools.demo_phone(vid))
            tampered.pop(vid, None)
            demo_tools.ensure_worker(db, demo_tools.demo_phone(vid))
            return {"reset": True}

        @app.post("/demo/speak")
        def demo_speak(request: Request, text: str = Form(...)):
            demo_worker(request)
            if ai.client is None:
                raise HTTPException(503, "Voice replies need GEMINI_API_KEY")
            throttle(request, speak_limit)
            wav = ai.speak(speakable(text[:2000]))
            if not wav:
                raise HTTPException(502, "Text-to-speech is not available right now")
            return Response(wav, media_type="audio/wav")

    # ------------------------------------------------------------------ chat simulator (demo without WhatsApp)
    @app.get("/simulator")
    def simulator(request: Request, user: str = Depends(staff)):
        return tpl.TemplateResponse(request, "simulator.html", {"ai": ai.enabled})

    @app.post("/simulator/send")
    async def simulator_send(phone: str = Form(...), text: str = Form(""),
                             file: UploadFile | None = File(None), user: str = Depends(staff)):
        msg = Inbound(phone=phone.strip(), text=text.strip() or None, source="simulator")
        if file is not None and file.filename:
            data = await file.read()
            if len(data) > 16 * 1024 * 1024:
                raise HTTPException(413, "file too large")
            mime = (file.content_type or "").split(";")[0]
            if mime.startswith("audio/"):
                msg.audio, msg.audio_mime = data, mime
            elif mime.startswith("image/"):
                msg.image, msg.image_mime = data, mime
        replies = bot.handle(msg)
        return JSONResponse([{"text": r.text, "document_url": r.document_url} for r in replies])

    # ------------------------------------------------------------------ helpers
    def _doc_redirect(outcome):
        r = outcome.reply
        if r.document_url:
            return RedirectResponse(r.document_url.replace(s.public_base_url, ""), status_code=303)
        raise HTTPException(400, r.text)

    return app


def _404():
    raise HTTPException(404)


def _phone(raw: str) -> str | None:
    """Supervisor phone as WhatsApp sends it: digits with country code. A 10-digit Indian number gets 91."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return None
    if len(digits) == 10:
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        raise HTTPException(400, "phone number should have 10 digits, or a country code and number")
    return digits


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
