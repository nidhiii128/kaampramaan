# KaamPramaan: pitch kit

Fill every `[FILL]` with a real fact before presenting. Do not present a number you cannot source.

## One line

Welfare boards hold tens of thousands of crore for construction workers but can only pay registered ones, and
registration needs proof of 90 days of work that most workers cannot produce. KaamPramaan makes that proof: a one-tap
QR check-in at the site gate, confirmed by the supervisor, ready for the board.

## The numbers (sourced, say them exactly)

- ₹70,744 crore of construction welfare cess unspent (Labour Ministry RTI reply, January 2025).
- Registration and renewal need 90 days of construction work in the preceding 12 months. The rule continues under the
  Code on Social Security, in force since 21 Nov 2025.
- CAG audits of state boards report gaps in registering workers and establishments. `[FILL: one line from the CAG
  report for the state you pitch in]`

## 3-minute structure

| Time | Beat | Say / show |
|---|---|---|
| 0:00 | A person | One named worker (real if you can, else clearly labelled as a composite). He worked 8 months on a tower. The board form asks for 90 days of proof, and the contractor won't sign. The pension, accident cover and his daughter's scholarship are all out of reach. |
| 0:25 | The money | ₹70,744 crore collected for workers like him, unspent. The bottleneck is not money, it is proof. |
| 0:45 | Live demo (90 s) | See "Demo script" below. |
| 2:15 | Why it works | Nothing to install: a QR poster and WhatsApp. The employer side vouches without new software. Gemini reads the paper register the site already keeps. Every day is tamper-evident. |
| 2:35 | Who pays | The board, from money it already has (see "Business model"). |
| 2:50 | Ask | `[FILL: pilot district / board intro, incubator seat, cloud credits]` |

## Demo script (rehearse it; the demo is the pitch)

0. Open `/demo` (`KP_DEMO_MODE=1`). No login.
1. **Scan the site QR.** "This poster is at the gate. One tap in WhatsApp, and he is marked present."
2. **Supervisor: MUSTER, then CONFIRM.** "The supervisor sees today's list and confirms it. Now the day counts."
3. **Add 89 past days**, then send "registration". Open the PDF: the pre-filled board application, with all 90 days,
   who vouched for each, and the reference codes. "The officer fills identity and bank details at the counter."
4. **Change a past entry.** The record check turns red. "Nobody, including us, can quietly add days."
5. Switch to the seeded staff site (`python -m scripts.seed_demo`), `/board`: ready to register, needs confirmation,
   renewals due with the reminder button, records to check.
6. `/sites/<id>`: upload a photo of a handwritten muster register. Gemini reads it, you correct one row, and import.
   "Paper registers sites already keep become verified days."

Backup: a screen recording of steps 1 to 6. Everything except voice and the register photo works without a Gemini key.

## Business model

Workers never pay.

- **State welfare boards (primary).** They collect the cess, are audited on registration and spending, and lose money
  to fake registrations. We sell a per-registered-worker or per-site fee, procured through GeM after a pilot.
  `[FILL: only claim interest after a conversation with a board official]`
- **Facilitation partners: NGOs, unions, CSC operators.** They already run registration camps. They use the board
  view and register-photo import, funded by the board or by CSR.
- **Principal employers and developers (later).** Under the Code, employers must register eligible workers. A
  supervisor's CONFIRM is the cheapest compliance record they can keep.

Unit cost to know before you are asked: WhatsApp conversation charges per worker per month (check-ins are
user-initiated, which is the cheaper category), Gemini cost per register photo and per voice note. `[FILL: measured
from one day of testing]`

## Funding path

| Stage | Source | What we need for it |
|---|---|---|
| Now | Buildathon, Smart India Hackathon (labour ministry / state labour department problem statements), college incubator | Working demo, this repo |
| Prototype | DST NIDHI-PRAYAS, MeitY TIDE 2.0 (through host incubators), state startup grants | Incubator seat, short proposal |
| Pilot | MoU with a state labour department or board for one district, run with an NGO or union | One champion official, a partner NGO |
| Scale | Board procurement through GeM | DPIIT recognition (relaxed turnover and experience rules), pilot results |

Startup India Seed Fund closed to new applications on 31 May 2026. Watch for a new call; do not plan around it.

## Moat

Trust is the product. Each day is checked in by the worker and vouched for by the employer side. Attestations are
hash-linked to the entries they confirm, and every register photo is fingerprinted. And the data across sites (who
worked where, on which days, confirmed by whom) is something no single NGO or contractor has.

## Hard questions, straight answers

- **Why would a supervisor confirm?** It takes one WhatsApp message a day, and under the Code the employer side has to
  register eligible workers anyway. Where a supervisor won't, the paper register photo or a co-worker-backed day is the
  fallback, and the board decides which levels it accepts.
- **Can a supervisor and worker collude on fake days?** The QR check-in is per site and day. The board view flags the
  same person at two sites on one day and duplicate names. Every confirmation names the person who gave it. Fake
  certificates exist today too; the difference is that ours leave a trail.
- **What if Gemini misreads a register?** Nothing is saved until a person checks each row against the photo. Impossible
  dates are dropped, and every imported day carries the photo's fingerprint.
- **Is the application valid?** It is a pre-filled form plus a work certificate. The officer still checks identity and
  originals. A labour lawyer and one board official should review the format for the pilot state.
- **Privacy?** No Aadhaar or bank numbers are stored. Before the pilot: consent in the first message, per-board
  accounts, an audit log, retention limits, hosting in India (Cloud Run / Cloud SQL in asia-south1).
- **Keypad phones?** Workers without smartphones come in through the register photo or volunteer logging now, and a
  missed-call line next.
- **Why not just e-Shram?** e-Shram registers a person. It does not prove 90 days of construction work, which the board
  still needs. We link to the UAN; we don't replace it.

## Built on Google

Gemini vision (muster registers, site boards), audio understanding, function calling (8-tool agent), structured
output, embeddings and text-to-speech. Cloud Run in asia-south1. Scale path: Cloud SQL and Cloud Storage.

## Pilot plan (first 60 days after the buildathon)

1. One NGO or union and 3 to 5 sites in one district; posters at the gates; supervisors on WhatsApp.
2. Measure: check-in rate after week 2, share of days confirmed by supervisors, register photos imported and rows
   corrected, workers reaching 90 days, applications submitted, registrations granted.
3. One board official reviews the application format. One labour lawyer reviews the wording.
