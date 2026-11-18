"""FastAPI app: WhatsApp webhook, NGO website, chat simulator and document links."""
import logging
import os
import secrets
from datetime import date

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ledger
from .ai import AI
from .chat import Bot, Inbound
from .config import Settings, load_settings
from .db import Database
from .records import rupees, summarise
from .whatsapp import WhatsAppClient, parse_webhook, verify_signature

log = logging.getLogger("kaampramaan")
HERE = os.path.dirname(__file__)


def create_app(settings: Settings | None = None, ai: AI | None = None, today_fn=None) -> FastAPI:
    s = settings or load_settings()
    today_fn = today_fn or s.today
    db = Database(s.db_path)
    ai = ai or AI(s.gemini_api_key, s.gemini_model, s.gemini_fallback_models)
    bot = Bot(db=db, ai=ai, media_dir=s.media_dir, public_base_url=s.public_base_url, today_fn=today_fn)
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
            for r in bot.handle(msg):
                if r.document_url:
                    wa.send_document(m.phone, r.document_url, r.document_name or "record.pdf", r.text)
                else:
                    wa.send_text(m.phone, r.text)
        except Exception:
            log.exception("failed to process WhatsApp message %s", m.message_id)
            wa.send_text(m.phone, "Sorry, kuch galat ho gaya. Something went wrong, please try again.")

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
        return tpl.TemplateResponse(request, "dashboard.html",
                                    {"rows": rows, "totals": totals, "cases": db.list_cases(), "ai": ai.enabled})

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
