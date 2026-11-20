# KaamPramaan

Verified 90-day work records for construction workers, so that state welfare boards can register them, and the money
already collected for them reaches them.

## The problem

Every construction project in India pays a 1% welfare cess to the state Building and Other Construction Workers'
Welfare Board. That fund pays for pensions, accident cover, maternity benefit, children's education and housing
assistance, but only for **registered** workers. To register (and to renew every year), a worker must show **90 days
of construction work in the preceding 12 months**. This rule carries over under the Code on Social Security, which
came into force on 21 Nov 2025.

Most workers are paid in cash by a contractor and have no payslip, so they cannot prove those 90 days. The certificate
usually has to come from an employer, union or official, so a middleman often ends up controlling it. Boards cannot
tell real workers from fake registrations, and the money sits unused. A January 2025 RTI reply from the Labour
Ministry put **₹70,744 crore of cess unspent**. CAG audits of state boards keep pointing at gaps in registering
workers and establishments.

## What KaamPramaan does

```
 Site gate                 Supervisor                    Welfare board / NGO                 Worker
 ─────────                 ──────────                    ───────────────────                 ──────
 QR poster  ── scan ──►    WhatsApp: MUSTER, CONFIRM     Board view: ready to register,      Pre-filled application
 "HAZIRI K7M2QX"           or a photo of the paper  ──►  needs confirmation, renewals,  ──►  + 90-day certificate PDF,
 (1 tap, no app)           muster register (Gemini)      records to check                    renewal reminders
```

1. **Check-in, one tap.** Each site gets a printable QR poster (`/sites/<id>/poster`). Scanning it opens WhatsApp with
   `HAZIRI <site code>` already typed, so the worker just presses Send. A new worker gives their name once, by voice or
   text. There is no app to install and nothing to read. Workers without phones are added from the register (step 3)
   or by a volunteer.
2. **The employer side vouches.** The site supervisor sends `MUSTER` on WhatsApp to see today's list and `CONFIRM`
   (or `CONFIRM 1 3`) to vouch for it. Supervisors who don't use WhatsApp can be confirmed for by an officer on the
   website.
3. **Paper registers count too.** Most sites already keep a handwritten muster register. The supervisor (on WhatsApp)
   or an officer (on the website) sends a photo. **Gemini vision** reads it into names and days present. A person
   checks every row against the photo, and only then is anything saved. Each imported day carries the photo's SHA-256.
4. **Board view** (`/board`) shows the work queue:
   - who is ready to register now (90+ employer-confirmed days);
   - who has enough days but needs their employer to confirm them;
   - who is close to 90;
   - whose registration is due for renewal, with a WhatsApp reminder button;
   - records to check: the same worker at two sites on one day, or the same name and village registered twice.
5. **Pre-filled application.** One click (or the worker sending "registration") produces the application with the
   90-day work certificate. It lists every working day, who vouched for it and its tamper-proof reference.
   Identity, bank and nominee details are left blank on purpose, for the officer to check against originals.

Every working day gets one of three levels, and only the first counts towards "ready to register" by default:

| Level | Meaning |
|---|---|
| Confirmed by employer side | The site supervisor confirmed it, the day is on the site's own muster register, or an officer confirmed it |
| Backed by co-workers | 2 or more other workers checked in at the same site on the same date |
| Worker's own report | Only the worker says so |

**Also included, for NGO partners:** wage complaints from the same record ("Ramesh ne paisa nahi diya" turns into a
complaint PDF, which is tracked through filed and recovered), and work matching ("kaam chahiye").

## Try it (no login)

Set `KP_DEMO_MODE=1` and open `/demo`. Each visitor gets a private sandbox:

1. Press **Scan the site QR**. That is the worker's check-in.
2. In the supervisor's WhatsApp panel, press **Send MUSTER**, then **Send CONFIRM**. The day now counts as confirmed.
3. Press **Add 89 past days, already confirmed**, then send "registration". You get the 90-day application PDF.
4. Press **Change a past entry** and watch the record check catch the tampering.

Typing and the microphone both work (Hindi, Hinglish, Bhojpuri and others). Demo data is kept out of the board view
and all statistics, rate-limited per IP and deleted after 24 hours.

## For evaluators: where to find each criterion

| Criterion | Evidence |
|---|---|
| Functional product (30%) | End-to-end flow working in `/demo` with no setup. Staff site: `/board`, `/sites`, `/sites/<id>/poster`, `/muster/<id>`, `/workers/<id>`. 116 automated tests, including the full QR → supervisor → muster → board → PDF → renewal flow (`tests/test_board.py`, `tests/test_demo_board.py`). `python -m scripts.seed_demo` fills the board view with sample data. |
| Technical architecture (20%) | Channel-independent `Bot` (`app/chat.py`) serves WhatsApp, the simulator and the demo. Append-only, hash-chained ledger (`app/ledger.py`). Attestations stored separately and hash-linked to the entry they vouch for (`app/attest.py`). Pure-function verification and board rules (`app/records.py`, `app/board.py`). Schema migrations for older databases (`app/db.py`). CI runs lint, tests, offline evals and a Docker build (`.github/workflows/ci.yml`). |
| Google AI integration (20%) | Gemini is used six ways (table below): audio understanding, a tool-calling agent, structured output, **vision for handwritten muster registers and site boards**, embeddings, and text-to-speech. Guardrails: Gemini never does arithmetic, any reply containing a number no tool produced is rejected, register reads are range-checked and reviewed by a person, and model fallback means a busy API never loses an entry. |
| Problem-solution alignment (15%) | The board's own bottleneck, proving 90 days, is the product. The customer is the welfare board, which already holds the money (see "Who pays"). |
| UX, usability, accessibility (10%) | One-tap QR check-in, voice in and voice out (Gemini TTS) for workers who don't read comfortably, and bilingual Hindi/English text. The website has a skip link, labels, table captions, 44 px touch targets, a visible focus ring, `aria-live` chat logs and reduced-motion support. The poster prints for the site gate. |
| Innovation (5%) | Two-sided verification (worker check-in plus employer confirmation) without asking employers to install anything. Gemini turns the paper register the site already keeps into verified, fingerprinted days. Tamper-evidence the worker can hold too (a WhatsApp reference for every entry). |

## Who pays

Workers never pay. The welfare boards already collect the cess and need exactly this: more genuine registrations and
renewals, fewer fake ones, and an audit trail. The funding path we are pursuing:

- **Now:** buildathon, Smart India Hackathon, college incubator, state startup grants.
- **Prototype grants:** DST NIDHI-PRAYAS and MeitY TIDE 2.0 (through their host incubators). Startup India Seed Fund
  closed to new applications on 31 May 2026; we will apply if a new call opens.
- **Pilot:** one district, under an MoU with the state labour department or welfare board, run with an NGO or union
  that already holds registration camps.
- **Scale:** board procurement through GeM, priced per registered worker or per site.

Details, pitch and hard questions: [docs/PITCH.md](docs/PITCH.md).

## How it works

```
WhatsApp Cloud API ──► POST /webhook ──► signature check, de-duplication ──► background task
                                                                              │
Web simulator / public demo ─────────────────────────────────────────────────►├─► Bot (app/chat.py)
                                                                              │     HAZIRI check-in, supervisor MUSTER /
Website: sites, muster review, officer confirm, volunteer logging ───────────►│     CONFIRM / register photo, worker intents
                                                                              ▼
   Gemini (app/ai.py): voice/text -> tool calls -> reply; muster register photo -> rows; site board photo -> owner
   Ledger (app/ledger.py): append-only, SHA-256 hash chain per worker
   Attestations (app/attest.py): supervisor / muster register / officer, each hash-linked to the entry it vouches for
   Muster (app/muster.py): range-check Gemini's reading, human review, import with photo fingerprint
   Records (app/records.py): days, wages, verification level per day, 90-day rule
   Board (app/board.py): ready / needs confirmation / close / renewals / flags
   PDFs (app/pdfs.py): pre-filled board application with 90-day certificate, wage complaint
   QR (app/qr.py): wa.me check-in links and SVG posters
```

Design choices worth knowing:

- **Verification is added, never edited in.** A supervisor's CONFIRM, a muster register import or an officer's tick
  is a separate attestation row. Its hash covers the hash of the entry it vouches for, so moving it to another entry,
  or changing the entry afterwards, is detected (`verify_attestations`). Confirming twice does nothing.
- **A check-in is attendance, not money.** QR check-ins are saved with amount 0. A worker can still say "700 aaj, abhi
  nahi mila" by voice. That adds the wage but not a second day, so complaint totals and day counts stay right.
- **Gemini reads, Python decides, a person approves.** Muster register reads are range-checked: no impossible day
  numbers, no future dates, no month more than a year back, no blank names. Rows are shown next to the photo for
  correction. Names Gemini recognises are matched to known workers at that site, and unknown names become new
  workers without a phone.
- **Gemini is an agent with tools, but never does the maths.** For a daily message Gemini picks from eight tools
  (`log_work`, `record_payment`, `void_last_entry`, `get_balance`, `make_complaint`, `make_work_record`, `find_work`,
  `report_death`) and can call more than one in a message. The tools run in plain Python against the ledger. Gemini
  then writes a short reply in the worker's language. Its reply is rejected if it contains any number that did not
  come from a tool.
- **Tamper-evident ledger.** Each entry stores the hash of the previous entry for that worker, plus a SHA-256 of any
  attached voice note or photo. Editing, deleting or reordering an entry, or swapping a file, is detected by
  `verify_chain()` and shown on the website and in every PDF. Appends take a write lock, so concurrent messages cannot
  fork the chain.
- **Mistakes can be cancelled, never edited.** "galat" appends a `void` entry pointing at the mistaken one. Only
  entries from the last 24 hours can be cancelled this way.
- **The worker holds a proof too.** Each reply carries a short reference (the start of the entry's hash), inside a
  WhatsApp message timestamped by Meta.
- **No Aadhaar stored.** The application leaves identity, bank and nominee details for the officer to check against
  originals, which keeps the data held to the minimum.
- **Fake-record checks are reasons to look, not verdicts.** "Same day, two sites" and "same name and village" are
  listed for an officer; nothing is rejected automatically.
- **Webhook safety.** Meta's `X-Hub-Signature-256` HMAC is verified, message IDs are de-duplicated and processing runs
  in the background. If every Gemini model fails, text messages still go through the offline parser.

## How Gemini is used

| Capability | Where | Why |
|---|---|---|
| Vision: handwritten muster registers | `ai.read_muster`, `app/muster.py` | Turns the paper register a site already keeps into reviewed, verified working days, with structured output and known names passed in for matching. |
| Vision: site boards | `ai.read_board` | A photo of the project board identifies the principal employer for the application and complaints. |
| Audio understanding | `ai.understand`, `ai.run_agent` | Voice notes in Hindi, Bhojpuri, Bengali, Marathi or English go straight to Gemini, with no separate speech-to-text step. |
| Function calling (agent) | `ai.run_agent`, 8 tools | Gemini chooses which tool to run, even several in one message ("galat, 700 tha"). |
| Structured output | `understand`, `read_board`, `read_muster` | JSON schemas for intents, amounts, board details and register rows. |
| Embeddings | `app/recommend.py` | Matches "rang rogan ka kaam" to work openings by meaning. |
| Text-to-speech | `ai.speak`, `app/voice.py` | Spoken replies for workers who don't read comfortably (`KP_VOICE_REPLIES=1`), and a Listen button in the demo. |

Check your key and each capability: `python -m scripts.check_gemini --agent --tts --image board.jpg --muster register.jpg`.

**Measured, not claimed.** `python -m scripts.eval_extraction` runs 34 hand-written messages in five languages through
the extraction step. Offline rule-based parser baseline (no Gemini), 20 Nov 2026: English 88%, Hinglish 100%,
Bhojpuri 0%, Bengali 20%, Marathi 0%; whole message right 44%. Number words like "saat sau" and non-Hindi dialects are
where rules fail, which is the case for Gemini. Run it with your key to fill in Gemini's numbers. The cases were
written by the team, not collected from real workers. Muster register reading has no labelled photo set yet. Collecting
one during the pilot, with the supervisors' consent, is the first evaluation task.

Work matching (`python -m scripts.eval_recommend`, 14 made-up labelled cases, 19 Nov 2026):

| Embeddings | Variant | Top-1 | Top-3 | MRR |
|---|---|---|---|---|
| gemini-embedding-001 | meaning only | 86% | 93% | 0.91 |
| gemini-embedding-001 | full ranker | 93% | 93% | 0.95 |
| offline hashing | meaning only | 57% | 71% | 0.68 |
| offline hashing | full ranker | 71% | 86% | 0.80 |

## Run it locally

Needs Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set KP_ADMIN_PASSWORD, GEMINI_API_KEY, and KP_WA_NUMBER for the QR posters
python -m scripts.seed_demo     # optional: sample sites, workers and confirmations for the board view
uvicorn app.main:create_app --factory --reload --port 8000
```

Open http://localhost:8000 and log in with KP_ADMIN_USER / KP_ADMIN_PASSWORD.

- **Board view**: ready to register, needs confirmation, renewals, records to check
- **Sites**: add a site and its supervisor, print the QR poster, confirm a day, upload a muster register photo
- **Workers**: each worker's days, verification levels, record check, application and complaint PDFs
- **Log for a group**, **Work openings**, **Chat simulator** (the WhatsApp conversation in the browser, same code path)

Run the tests: `pytest -q`

## Connect a real WhatsApp number

1. Create an app at developers.facebook.com, add the WhatsApp product and use the free test number.
2. Put the access token, phone number ID and app secret in `.env`, choose any `WA_VERIFY_TOKEN`, and set
   `KP_WA_NUMBER` to the number's digits (for the QR posters).
3. Expose your server (for example `ngrok http 8000`) and set `KP_PUBLIC_BASE_URL` to that HTTPS URL.
4. In the app's WhatsApp configuration, set the webhook URL to `<public URL>/webhook`, use the same verify token,
   and subscribe to `messages`.
5. Add a site with your phone as its supervisor, print its poster, scan it from a second phone.

## Deploy on Google Cloud Run

```bash
gcloud run deploy kaampramaan --source . --region asia-south1 --allow-unauthenticated \
  --set-env-vars KP_ADMIN_USER=admin,GEMINI_MODEL=gemini-3.8-flash \
  --set-secrets KP_ADMIN_PASSWORD=kp-admin:latest,GEMINI_API_KEY=gemini-key:latest,WA_ACCESS_TOKEN=wa-token:latest,WA_APP_SECRET=wa-secret:latest
```

The region `asia-south1` (Mumbai) keeps data in India. This version stores data in SQLite on local disk, which Cloud
Run does not keep between restarts. For a pilot, move storage to Cloud SQL and media to Cloud Storage (see Roadmap).

## Project layout

```
app/
  main.py        FastAPI routes: webhook, board view, sites, posters, muster review, worker pages, demo
  chat.py        conversation logic: worker check-in and intents, supervisor MUSTER / CONFIRM / register photo
  ai.py          Gemini agent with tools, vision (muster registers, site boards), schemas, number guard, offline parser
  ledger.py      hash-chained entries and verification
  attest.py      supervisor / muster register / officer attestations, hash-linked to entries
  muster.py      range checks, review and import for muster register photos
  records.py     totals, unpaid days, verification level per day, 90-day rule
  board.py       welfare board queues: ready, needs confirmation, close, renewals, flags
  pdfs.py        pre-filled board application with 90-day certificate; wage complaint
  qr.py          check-in links and SVG QR codes
  recommend.py   work matching: embeddings, ranking, personalisation
  voice.py       Gemini TTS audio to WAV / ogg-opus
  demo.py        public sandbox: visitor ids, sandbox supervisor, fast-forward, rate limiter
  whatsapp.py    Cloud API parsing, HMAC check, media download, sending
  db.py          SQLite schema, migrations and queries
  templates/     website pages, QR poster, demo
scripts/         seed data, Gemini check, offline and Gemini evaluations
.github/         CI: lint, tests, offline evals, Docker build
tests/           ledger, attestations, check-in, supervisor, muster import, board view, PDFs, demo, agent, webhook
```

## Legal wording

The application does not cite a section of the BOCW Act 1996, because the Labour Codes replaced it on 21 Nov 2025.
It says "90 days in the preceding 12 months (confirm the current requirement with your state building workers'
welfare board)". The renewal period is a setting (`RENEWAL_DAYS` in `app/board.py`) because it differs by state.
A labour lawyer and one board official should review the application format before real use. The application
supports an officer's decision; it does not replace it.

## Roadmap

- Pilot with one board or district: measure registrations completed, renewals saved from lapsing, time to register,
  and duplicates caught
- Collect a consented, labelled set of muster register photos and report Gemini's row and day accuracy
- Export in each state board's own application format, and an API for the board's portal
- Missed-call / IVR check-in for keypad phones
- Cloud SQL and Cloud Storage instead of SQLite and local files; per-board accounts and an audit log
- Bhashini as an additional language option alongside Gemini
- Publish a daily root hash of all ledgers, so even the operator cannot rebuild history unnoticed
