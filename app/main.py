"""FastAPI app: WhatsApp webhook, NGO website, chat simulator and document links."""
import logging
import os
import secrets
from datetime import date

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import demo as demo_tools
from . import ledger
from .ai import AI
from .chat import Bot, Inbound
from .config import Settings, load_settings
from .db import WAGE_CASE_STATUSES, Database
from .recommend import CachedEmbedder, GeminiEmbedder
from .records import live_entries, rupees, summarise
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
        return RedirectResponse("/dashboard")

    @app.get("/dashboard")
    def dashboard(request: Request, user: str = Depends(staff)):
        rows = []
        for w in db.list_workers():
            sm = summarise(db.list_entries(w["id"]), today=today_fn())
            rows.append({"w": w, "s": sm})
        totals = {
            "workers": len(rows),
            "due": sum(r["s"].due for r in rows),
            "eligible": sum(1 for r in rows if r["s"].welfare_eligible),
            "days": sum(r["s"].days_worked for r in rows),
        }
        return tpl.TemplateResponse(request, "dashboard.html", {
            "rows": rows, "totals": totals, "cases": db.list_cases(), "ai": ai.enabled,
            "impact": db.impact_stats(), "wage_cases": db.list_wage_cases(), "statuses": WAGE_CASE_STATUSES})

    @app.get("/workers/{worker_id}")
    def worker_page(worker_id: int, request: Request, user: str = Depends(staff)):
        w = db.get_worker(worker_id) or _404()
        entries = db.list_entries(worker_id)
        return tpl.TemplateResponse(request, "worker.html", {
            "w": w, "site": db.get_site(w["site_id"]), "entries": entries,
            "s": summarise(entries, today=today_fn()),
            "chain": ledger.verify_chain(db, worker_id, media_reader=_read_file),
            "corr": db.corroboration_counts(worker_id),
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
            demo_tools.ensure_worker(db, demo_tools.demo_phone(vid))
            resp = tpl.TemplateResponse(request, "demo.html", {"ai": ai.client is not None})
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
            chain = ledger.verify_chain(db, worker["id"], media_reader=_read_file)
            return {
                "chain": {"ok": chain.ok, "entries_checked": chain.entries_checked,
                          "broken_at_seq": chain.broken_at_seq, "reason": chain.reason},
                "summary": {"days": sm.days_worked, "earned": sm.earned, "received": sm.paid, "due": sm.due,
                            "cancelled": sm.cancelled_entries},
                "entries": [{"seq": e["seq"], "kind": e["kind"], "date": e["work_date"], "amount": e["amount"],
                             "paid": bool(e["paid"]), "contractor": e["contractor"], "ref": e["hash"][:12],
                             "said": e["transcript"], "void_of": e["void_of"]} for e in entries][-40:],
                "tampered": visitor_id(request) in tampered,
            }

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
    def _doc_redirect(replies):
        r = replies[0]
        if r.document_url:
            return RedirectResponse(r.document_url.replace(s.public_base_url, ""), status_code=303)
        raise HTTPException(400, r.text)

    return app


def _404():
    raise HTTPException(404)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
