# KaamPramaan: pitch kit

Fill every `[FILL]` with a real fact before presenting. Do not present a number you cannot source.

## One line

A construction worker sends one WhatsApp voice note a day. We keep a tamper-evident record of it, and when a
contractor does not pay, that record becomes the complaint.

## 3-minute structure

| Time | Beat | Say / show |
|---|---|---|
| 0:00 | A person, not a statistic | One named worker (real if you can, else a clearly labelled composite). Cash wages, no paper, contractor stops paying, he has nothing to show. |
| 0:25 | Why now | The Labour Codes came into force on 21 Nov 2025, so registration and wage-payment rules are being rebuilt right now. `[FILL: one sourced line on what changed for construction workers]` |
| 0:45 | Live demo (90 s) | See "Demo script" below. |
| 2:15 | Why this can work | WhatsApp = nothing to install. Voice = no literacy needed. Gemini hears Hindi/Bhojpuri/Bengali audio directly. Money maths is plain Python, never the model. |
| 2:40 | Who pays | See "Business model". |
| 2:55 | Ask | `[FILL: pilot partner, mentorship, credits, intro to a union/NGO]` |

## Demo script (rehearse this, it is the whole pitch)

0. Judges can do this themselves: open `/demo` (set `KP_DEMO_MODE=1`), no login, microphone built in, the stored record shown beside the chat.
1. `/demo` or real WhatsApp: voice note "Aaj Ramesh ke saath kaam kiya, 700, abhi nahi mile". Reply shows days, amount due and a `Ref:`.
2. Say it wrong on purpose ("7000"), then send "galat, 700 tha". Show it cancelled and re-logged in one message. Point out: nothing was deleted.
3. "Ramesh ne paisa nahi diya" -> complaint PDF arrives. Open it: unpaid days, co-worker counts, reference codes, "Verified: all entries intact".
4. **Tamper moment.** On `/demo` press **Change a past entry**: an entry is edited behind the system's back and the record check turns red and names the broken entry. This is the proof that the record cannot be quietly rewritten. Press **Put it back** to heal it.
5. "Kaam chahiye" -> top 3 openings, one with a warning that workers reported unpaid wages from that contractor.
6. Staff dashboard: the complaint is already a tracked case. Mark it filed, record Rs. 1,400 recovered: the worker's balance drops, they get a WhatsApp message, and the **wages recovered** tile moves. That is the outcome the whole system exists for.
7. Press **Listen** on a reply (or send a voice note with `KP_VOICE_REPLIES=1`): Gemini answers in speech, for workers who do not read comfortably.

If WhatsApp or Gemini fails on stage: the simulator and the offline parser run the same code path. Keep a screen recording as backup.

## Business model (what to say, not yet validated)

Workers never pay. Payers are the people who need a trustworthy record:

- **Principal employers and developers.** Under the new codes they carry compliance and liability for contractors' wage defaults. A per-site subscription gives them a verifiable muster record and early warning on contractors who are not paying.
- **CSR funds, unions, NGOs.** Grant-funded dashboard seats; they already run legal-aid and welfare-registration drives and currently collect this by hand.
- **State welfare boards.** Cleaner registration evidence, fewer rejected applications. `[FILL: only claim this after talking to one]`
- **Lenders and insurers (later).** A worker-owned income and days-worked record is something informal workers cannot produce today.

Unit cost to know before you are asked: Gemini audio + tool calls per worker per month, and WhatsApp conversation charges. `[FILL: measured numbers from a day of testing]`

## Moat

The record is only useful if it is trusted, so trust is the product: a hash-chained log, cancellations that never delete, a reference the worker holds in WhatsApp, and (once you have it) legal sign-off on the PDFs. The cross-worker contractor payment record is a data asset no single NGO has.

## Hard questions, straight answers

- **Is this admissible?** It is the worker's own contemporaneous account plus a tamper-evident log and corroboration from co-workers. It supports a complaint to a labour officer; it does not replace an employer's records. A labour lawyer should review the PDFs. The welfare PDF says "confirm with your state board" instead of citing a repealed Act.
- **What if the model mishears?** Every save is echoed back with the amount and a one-word undo. The model never does the arithmetic, and a reply containing a number the ledger did not produce is discarded.
- **What if the operator edits the database?** Locally the chain would break (shown in the demo). A server operator could rebuild a whole chain, which is why each entry's reference is also sent to the worker on WhatsApp. Next step: publish a daily root hash.
- **Retaliation?** Complaints are generated only on the worker's request; the contractor is not notified. Contractor warnings are worded as reports, need 2+ workers and 3+ unpaid days, and are not shown with names of the workers. Stronger dispute and safety handling is on the roadmap.
- **Privacy?** Phone, village, GPS and voice notes are sensitive. Needed before a real pilot: explicit consent in the first message, per-NGO accounts, an audit log, retention limits.
- **Why WhatsApp?** It is already on their phone. Keypad-phone workers are served by volunteer logging now and a missed-call voice line next.
- **How do you stop fake reports?** Co-worker corroboration on site and date, board photo for the site, volunteer-attested entries marked as such. Sybil resistance is open work.

## Built on Google

Gemini audio understanding (no separate speech-to-text), Gemini function calling for the agent (8 tools), Gemini vision for site boards, Gemini embeddings for work matching, Gemini text-to-speech for voice replies, Cloud Run for the app. Extraction accuracy is measured by `scripts/eval_extraction.py` (offline baseline 44% whole-message; fill in the Gemini number after running it with your key). Scale path: Cloud SQL or Firestore for the ledger, Cloud Storage for voice notes and PDFs.

## Pilot plan (the first 30 days after the buildathon)

1. One NGO or union, 30-50 workers on 1-2 sites.
2. Measure: share of workers still logging after day 7, days logged per worker, complaints generated, rupees recovered.
3. One labour lawyer reviews both PDFs. One welfare-board official reacts to the work record.
