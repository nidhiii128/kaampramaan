# KaamPramaan

Proof of work for construction and migrant workers, kept through WhatsApp.

Most construction workers in India are paid in cash, with no contract or payslip. When a contractor
does not pay, the worker has nothing to show. The same missing record stops them from registering
with the state welfare board (which needs 90 days of work in the last 12 months), and stops their
family from claiming benefits if something happens to them.

KaamPramaan lets a worker send a short voice note every evening. It keeps a tamper-evident record
of each working day and turns it into a wage complaint or a welfare board work record when needed.

## What it does

| Who | How | What they get |
|---|---|---|
| Worker with a smartphone | WhatsApp voice note or text, photo of the site board | Daily confirmation, running total of days and money due, complaint PDF |
| Worker without a phone | A volunteer logs the group on the website | Same record, marked "logged by volunteer" |
| NGO, union, family | Website | Each worker's history, unpaid days, record check, PDFs |

Conversation example (worker side):

```
Worker: Worked with Ramesh today, 700 not paid yet
Bot:    Saved: 18 Nov, Rs. 700 (not paid yet). This month: 12 days. Due: Rs. 4,200.
Worker: Ramesh has not paid me
Bot:    You are owed Rs. 4,200 for 6 days. [KaamPramaan_complaint.pdf]
```

## How it works

```
WhatsApp Cloud API ──► POST /webhook ──► signature check, de-duplication ──► background task
                                                                              │
Web chat simulator ──► POST /simulator/send ─────────────────────────────────►├─► Bot (app/chat.py)
                                                                              │     registration, intents
Volunteer form ─────► POST /volunteer ───────────────────────────────────────►│
                                                                              ▼
                          Gemini agent (app/ai.py): voice/text -> tool calls -> reply, site board photo -> owner
                          Ledger (app/ledger.py): append-only, SHA-256 hash chain per worker
                          Records (app/records.py): days worked, FIFO payment matching, welfare eligibility
                          PDFs (app/pdfs.py): wage complaint, welfare board work record
```

Design choices worth knowing:

- **Gemini is an agent with tools, but never does the maths.** For a daily message Gemini picks from six tools
  (`log_work`, `record_payment`, `get_balance`, `make_complaint`, `make_work_record`, `report_death`) and can call
  more than one, for example "worked yesterday for 700, and Ramesh gave 500 today". The tools run in plain Python
  against the ledger and their results go back to Gemini, which writes a short reply in the worker's language.
  Tool arguments are range-checked before anything is saved. All money and eligibility calculations are plain Python,
  so every number can be traced to ledger entries.
- **Number guard.** Gemini's reply to the worker, and its wording of the complaint paragraph, are rejected if they
  contain any number that did not come from a tool call or the computed facts. The worker then gets the tool's fixed
  reply instead.
- **No double logging.** If Gemini fails before any tool runs, the message goes through the older intent extractor
  and the offline parser. If it fails after a tool has run, the fixed replies for the tools that already ran are sent,
  so a day is never saved twice.
- **Tamper-evident ledger.** Each entry stores the hash of the previous entry for that worker, plus a SHA-256 of any
  attached voice note or photo. Editing, deleting or reordering an entry, or swapping a file, is detected by
  `verify_chain()` and shown on the website and in every PDF. Appends take a write lock (`BEGIN IMMEDIATE`) so
  concurrent messages cannot fork the chain.
- **Payments are matched to the oldest unpaid days first**, so the complaint lists exactly which days are unpaid.
- **Co-worker confirmation.** For each working day the website and PDFs show how many other workers logged the same
  site on the same date.
- **Webhook safety.** Meta's `X-Hub-Signature-256` HMAC is verified, message IDs are de-duplicated because Meta retries
  deliveries, and the webhook answers immediately while processing runs in the background.
- **Model fallback.** If the main Gemini model is overloaded (503), rate-limited (429) or retired (404), the request
  moves to the next model in `GEMINI_FALLBACK_MODELS`. If every model fails, text messages still go through the
  offline parser, so a busy API never loses a worker's entry.
- **Works offline for development.** Without a Gemini key, a rule-based English/Hinglish parser handles text messages.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set KP_ADMIN_PASSWORD, and GEMINI_API_KEY if you have one (read automatically)
python -m scripts.seed_demo     # optional: sample workers for the website
uvicorn app.main:create_app --factory --reload --port 8000
```

Check your Gemini key and model. It prints what Gemini extracts from sample Hindi messages, and with `--agent`
which tools the agent calls and what it replies (against a throwaway database):

```bash
python -m scripts.check_gemini --agent --image board.jpg --audio note.ogg   # all flags are optional
```

Open http://localhost:8000 (log in with KP_ADMIN_USER / KP_ADMIN_PASSWORD).

- **Workers**: list, totals, family reports
- **Worker page**: record check, unpaid days, complaint and work-record PDFs, site details
- **Log for a group**: volunteer logging for workers without phones
- **Chat simulator**: the WhatsApp conversation in the browser, through the same code path as the webhook

Run the tests:

```bash
pytest -q
```

## Connect a real WhatsApp number

1. Create an app at developers.facebook.com, add the WhatsApp product and use the free test number.
2. Put the access token, phone number ID and app secret in `.env`, and choose any `WA_VERIFY_TOKEN`.
3. Expose your server (for example `ngrok http 8000`) and set `KP_PUBLIC_BASE_URL` to that HTTPS URL.
4. In the app's WhatsApp configuration, set the webhook URL to `<public URL>/webhook`, use the same verify token,
   and subscribe to `messages`.
5. Add your own phone as a test recipient and send "Hi".

## Deploy on Google Cloud Run

```bash
gcloud run deploy kaampramaan --source . --region asia-south1 --allow-unauthenticated \
  --set-env-vars KP_ADMIN_USER=admin,GEMINI_MODEL=gemini-3.8-flash \
  --set-secrets KP_ADMIN_PASSWORD=kp-admin:latest,GEMINI_API_KEY=gemini-key:latest,WA_ACCESS_TOKEN=wa-token:latest,WA_APP_SECRET=wa-secret:latest
```

Note: the current version stores data in SQLite on local disk, which Cloud Run does not keep between restarts.
For a real deployment, move storage to Firestore or Cloud SQL and media to Cloud Storage (see Roadmap).

## Project layout

```
app/
  main.py        FastAPI routes: webhook, website, simulator, PDF links
  chat.py        conversation logic, shared by WhatsApp, simulator and future voice line
  ai.py          Gemini agent with tools, JSON schemas, number guard, offline parser
  ledger.py      hash-chained entries and verification
  records.py     totals, unpaid days, welfare eligibility
  pdfs.py        complaint and work-record PDFs
  whatsapp.py    Cloud API parsing, HMAC check, media download, sending
  db.py          SQLite schema and queries
  templates/     website pages
scripts/seed_demo.py
tests/           ledger, records, parser, agent tool calls, full conversation, webhook, website
```

## Roadmap

- Firestore and Cloud Storage instead of SQLite and local files
- Replies as voice notes in the worker's language (Gemini text-to-speech)
- Missed-call voice line for keypad phones
- Rules for each state's welfare board forms
- Family claim flow with OTP verification
