# KaamPramaan

Proof of work for construction and migrant workers, kept through WhatsApp.

Most construction workers in India are paid in cash, with no contract or payslip. When a contractor
does not pay, the worker has nothing to show. The same missing record stops them from registering
with the state welfare board (which needs 90 days of work in the last 12 months), and stops their
family from claiming benefits if something happens to them.

KaamPramaan lets a worker send a short voice note every evening. It keeps a tamper-evident record
of each working day and turns it into a wage complaint or a welfare board work record when needed.
The same record helps workers find their next job: openings are ranked so that contractors who have
not paid wages logged by other workers come lower.

## What it does

| Who | How | What they get |
|---|---|---|
| Worker with a smartphone | WhatsApp voice note or text, photo of the site board | Daily confirmation, running total of days and money due, complaint PDF |
| Worker without a phone | A volunteer logs the group on the website | Same record, marked "logged by volunteer" |
| NGO, union, family | Website | Each worker's history, unpaid days, record check, PDFs |
| Worker looking for work | "Kaam chahiye" on WhatsApp | Top three openings for them, with wage, place, contact and warnings |
| NGO, union, site supervisor | Website, "Work openings" | Post openings and see how often each was shown |

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
                          Recommender (app/recommend.py): embeddings -> retrieve -> rank -> personalise
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
- **Mistakes can be cancelled, never edited.** Every saved reply ends with a short proof reference (the first 12
  characters of the entry's hash, also printed in the PDFs) and "Galat hai? send galat". That appends a `void` entry
  pointing at the mistaken one, so the chain stays valid and the cancellation is on the record. Cancelled entries are
  excluded from totals, PDFs, co-worker counts and contractor records, and the PDFs say how many were cancelled. Only
  entries from the last 24 hours can be cancelled this way, so repeating "galat" cannot erase history. Gemini can
  cancel and re-log in one message ("galat, 700 tha"). Databases created before this feature are migrated on start.
- **The worker holds a proof too.** The reference arrives in a WhatsApp message, which carries a timestamp from Meta,
  so the worker has evidence that does not depend on this server's database.
- **Payments are matched to the oldest unpaid days first**, so the complaint lists exactly which days are unpaid.
- **Co-worker confirmation.** For each working day the website and PDFs show how many other workers logged the same
  site on the same date.
- **Webhook safety.** Meta's `X-Hub-Signature-256` HMAC is verified, message IDs are de-duplicated because Meta retries
  deliveries, and the webhook answers immediately while processing runs in the background.
- **Model fallback.** If the main Gemini model is overloaded (503), rate-limited (429) or retired (404), the request
  moves to the next model in `GEMINI_FALLBACK_MODELS`. If every model fails, text messages still go through the
  offline parser, so a busy API never loses a worker's entry.
- **Works offline for development.** Without a Gemini key, a rule-based English/Hinglish parser handles text messages.

## Finding work (recommendations)

When a worker says "kaam chahiye" (or the agent calls `find_work`), `app/recommend.py` ranks open openings in three steps:

1. **Retrieval.** The request and each opening are embedded with Gemini (`GEMINI_EMBED_MODEL`, query and document
   task types). The top 20 openings by cosine similarity go on to ranking. If the request is vague ("kaam chahiye"),
   the worker's own past work from their ledger is used as the query. Opening embeddings are cached in SQLite.
2. **Ranking.** Five signals, each 0 to 1, are combined with fixed weights:

   | Signal | Weight | From |
   |---|---|---|
   | match | 0.40 | Embedding similarity to the request (and to the worker's past work) |
   | distance | 0.20 | Worker's last shared location to the site |
   | wage | 0.15 | Opening's wage compared with what this worker usually earns |
   | reliability | 0.15 | Share of days logged against this contractor, by all workers, that were paid (FIFO, smoothed). Self-reported, so warnings say "workers have reported unpaid wages" and need 2+ workers and 3+ unpaid days |
   | familiar | 0.10 | This worker was paid by this contractor before (up), or is owed money by them (down, with a warning) |

3. **Logging.** Every shown result is stored in `recommendations` with its rank and score, so changes can be
   measured on real use later.

Without a Gemini key, or if the embedding call fails, a hashing embedder (words and character trigrams) is used for
both sides, so the feature still works.

**Evaluation.** `python -m scripts.eval_recommend` runs 14 made-up labelled cases (12 openings around Delhi NCR,
including Hindi-only requests like "rang rogan ka kaam" and vague ones like "kaam chahiye"). Results on 19 Nov 2026:

| Embeddings | Variant | Top-1 | Top-3 | MRR |
|---|---|---|---|---|
| gemini-embedding-001 | meaning only | 86% | 93% | 0.91 |
| gemini-embedding-001 | full ranker | 93% | 93% | 0.95 |
| offline hashing | meaning only | 57% | 71% | 0.68 |
| offline hashing | full ranker | 71% | 86% | 0.80 |

Contractor names are matched conservatively: "Ramesh" matches "Ramesh Yadav", but "Ramesh Yadav" and "Ramesh Sharma"
are different people.

Known weakness: a new contractor with no history gets a neutral reliability score, so a good opening from them can
lose to a known contractor's weaker match (the one Gemini miss, "bijli ka kaam aata hai").

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
  recommend.py   work recommendations: embeddings, ranking, personalisation
  whatsapp.py    Cloud API parsing, HMAC check, media download, sending
  db.py          SQLite schema and queries
  templates/     website pages
scripts/seed_demo.py
tests/           ledger, void/cancel, records, parser, agent tool calls, full conversation, webhook, website
```

## Legal wording

The welfare-board PDF no longer cites a section of the BOCW Act 1996: the four Labour Codes came into force on
21 Nov 2025 and the old Acts were repealed, so it says "90 days in the preceding 12 months (confirm the current
requirement with your state building workers' welfare board)". Have a labour lawyer or NGO check the PDFs against your
state's rules before real use. The records are the worker's own account, backed by a tamper-evident log; they support a
complaint, they do not replace an employer's muster roll.

## Roadmap

- Firestore and Cloud Storage instead of SQLite and local files
- Replies as voice notes in the worker's language (Gemini text-to-speech)
- Missed-call voice line for keypad phones
- Learn ranking weights from which recommended openings workers actually call
- Rules for each state's welfare board forms
- Family claim flow with OTP verification
