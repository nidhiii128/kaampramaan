"""PDF documents a worker, NGO or family can take to a labour office or welfare board."""
from datetime import date, timedelta

from fpdf import FPDF

from .attest import AttestationCheck
from .ledger import ChainCheck
from .records import LEVEL_TEXT, WELFARE_RULE_NOTE, Summary, Verification, live_entries, rupees

EMPLOYER_TYPE_TEXT = {
    "government": "Government project. The department is the principal employer.",
    "developer": "Private developer project. The developer is the principal employer.",
    "small": "Small private site.",
    "unknown": "Owner of the site not identified.",
}


def _t(s) -> str:
    """Core PDF fonts are Latin-1 only; replace anything else instead of crashing."""
    s = "" if s is None else str(s)
    return s.replace("₹", "Rs. ").replace("–", "-").replace("—", "-").encode("latin-1", "replace").decode("latin-1")


class _Doc(FPDF):
    def __init__(self, title: str):
        super().__init__(format="A4")
        self.doc_title = title
        self.set_auto_page_break(True, margin=18)
        self.set_margins(18, 18, 18)
        self.add_page()

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(110)
        self.cell(0, 6, _t(f"KaamPramaan record  |  page {self.page_no()}"), align="C")

    def h1(self, text):
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(20)
        self.multi_cell(0, 8, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def h2(self, text):
        self.ln(2)
        self.set_font("Helvetica", "B", 11)
        self.multi_cell(0, 6, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def para(self, text, size=10):
        self.set_font("Helvetica", "", size)
        self.set_text_color(30)
        self.multi_cell(0, 5.2, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def kv(self, rows):
        for k, v in rows:
            self.set_font("Helvetica", "B", 10)
            self.cell(55, 6, _t(k))
            self.set_font("Helvetica", "", 10)
            self.multi_cell(0, 6, _t(v), new_x="LMARGIN", new_y="NEXT")

    def table(self, headers, rows, widths):
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(235, 238, 242)
        for h, w in zip(headers, widths):
            self.cell(w, 7, _t(h), border=1, fill=True)
        self.ln()
        self.set_font("Helvetica", "", 9)
        for row in rows:
            for v, w in zip(row, widths):
                self.cell(w, 6, _t(v), border=1)
            self.ln()


def _worker_block(doc, worker, site):
    doc.kv([
        ("Worker", worker["name"] or "Not given"),
        ("Village", worker["village"] or "Not given"),
        ("Phone", worker["phone"] or "Logged by volunteer"),
        ("Site", (site["project"] if site else None) or "Not recorded"),
        ("Principal employer", (site["principal_employer"] if site else None) or "Not identified"),
        ("Site type", EMPLOYER_TYPE_TEXT[(site["employer_type"] if site else "unknown") or "unknown"]),
    ])


def _integrity(doc, chain: ChainCheck, cancelled: int = 0):
    doc.h2("How this record was kept")
    status = (f"Verified: all {chain.entries_checked} entries are intact."
              if chain.ok else f"WARNING: record check failed at entry {chain.broken_at_seq} ({chain.reason}).")
    doc.para(
        "Each entry was saved when it was reported, with the time it was received. Every entry carries a "
        "SHA-256 reference code that is linked to the previous entry, so changing or removing any past entry "
        "breaks the chain and is detected. " + status
        + (f" The worker cancelled {cancelled} earlier entr{'y' if cancelled == 1 else 'ies'} as mistakes; "
           "cancelled entries stay on the record but are excluded from every total and table here."
           if cancelled else "")
    )


def complaint_pdf(path, worker, site, summary: Summary, chain: ChainCheck,
                  corroboration: dict[int, int], paragraph: str, today: date | None = None):
    today = today or date.today()
    doc = _Doc("Wage complaint")
    doc.h1("Complaint regarding non-payment of wages")
    doc.para(f"Date: {today.strftime('%d %B %Y')}")
    doc.para("To,\nThe Labour Officer / Assistant Labour Commissioner\n"
             + ((site["principal_employer"] + " (principal employer)") if site and site["principal_employer"] else ""))
    _worker_block(doc, worker, site)
    doc.h2("Summary")
    doc.kv([
        ("Unpaid days", str(len(summary.unpaid_days))),
        ("Total wages earned", f"Rs. {rupees(summary.earned)}"),
        ("Total received", f"Rs. {rupees(summary.paid)}"),
        ("Amount due", f"Rs. {rupees(summary.due)}"),
    ])
    doc.h2("Statement")
    doc.para(paragraph)
    doc.h2("Unpaid days")
    rows = [
        (u.work_date, u.contractor or "-", rupees(u.wage), rupees(u.still_due),
         str(corroboration.get(u.entry_id, 0)), u.hash[:12])
        for u in summary.unpaid_days
    ]
    doc.table(["Date", "Contractor", "Wage", "Due", "Co-workers", "Reference"], rows, [26, 42, 22, 22, 22, 40])
    doc.para("Co-workers: number of other workers who recorded work at the same site on the same day.", size=8)
    _integrity(doc, chain, summary.cancelled_entries)
    doc.ln(6)
    doc.para("Signature / thumb impression of worker: ____________________")
    doc.output(path)


ROLE_TEXT = {"supervisor": "Supervisor", "muster_register": "Muster register", "officer": "Officer"}
SOURCE_TEXT = {"site_qr": "Site QR", "muster_photo": "Register photo", "whatsapp": "WhatsApp", "volunteer": "Volunteer",
               "simulator": "Simulator", "demo": "Demo", "ivr": "Phone call"}


def application_pdf(path, worker, site, sites: dict, entries, summary: Summary, verification: Verification,
                    chain: ChainCheck, attestations: AttestationCheck, today: date | None = None):
    """Pre-filled application for registration as a building worker, with the 90-day work certificate.

    Things KaamPramaan does not hold (date of birth, Aadhaar, bank account, nominee) are left as blanks for the
    facilitation centre or board officer to fill and check against originals."""
    today = today or date.today()
    v = verification
    doc = _Doc("Application")
    doc.h1("Application for registration as a building worker (pre-filled)\nwith 90-day work certificate")
    doc.para(f"Prepared: {today.strftime('%d %B %Y')}. For submission to the State Building and Other Construction "
             "Workers' Welfare Board or its facilitation centre.", size=9)

    doc.h2("A. Worker")
    doc.kv([
        ("Name", worker["name"] or "Not given"),
        ("Village / native place", worker["village"] or "Not given"),
        ("Mobile", worker["phone"] or "None (logged through supervisor or volunteer)"),
        ("Date of birth", "______________________"),
        ("e-Shram UAN / Aadhaar", "To be checked by the officer against the original (not stored by KaamPramaan)"),
        ("Bank account", "______________________"),
        ("Nominee", "______________________"),
    ])

    doc.h2("B. Establishment (current site)")
    doc.kv([
        ("Project", (site["project"] if site else None) or "Not recorded"),
        ("Principal employer", (site["principal_employer"] if site else None) or "Not identified"),
        ("Site type", EMPLOYER_TYPE_TEXT[(site["employer_type"] if site else "unknown") or "unknown"]),
        ("Contractor", (site["contractor"] if site else None) or "Not recorded"),
        ("Site supervisor", (f"{site['supervisor_name'] or 'Name not given'}, {site['supervisor_phone']}"
                             if site and site["supervisor_phone"] else "Not registered")),
        ("KaamPramaan site code", (site["code"] if site else None) or "-"),
    ])

    doc.h2("C. 90-day work certificate (last 12 months)")
    doc.kv([
        ("Confirmed by employer side", f"{v.employer_days_12m} days (supervisor confirmation or muster register)"),
        ("Backed by co-workers only", f"{v.coworker_days_12m} days"),
        ("Worker's own report only", f"{v.self_days_12m} days"),
        ("Rule", WELFARE_RULE_NOTE),
        ("Status", "Meets the 90-day requirement on employer-confirmed days" if v.ready_to_register
         else f"{v.verified_days_to_go} more employer-confirmed days needed"),
    ])

    doc.h2("D. Working days")
    year_ago = (today - timedelta(days=365)).isoformat()
    seen, rows = set(), []
    for e in sorted(live_entries(entries), key=lambda e: (e["work_date"], e["seq"])):
        d = e["work_date"]
        if e["kind"] != "work" or d in seen or not (year_ago < d <= today.isoformat()):
            continue
        seen.add(d)
        s = sites.get(e["site_id"])
        level = v.by_date.get(d, "self")
        who = ", ".join(ROLE_TEXT[r] for r in sorted(v.verifiers.get(d, ()))) or LEVEL_TEXT[level]
        rows.append((d, ((s["project"] if s else None) or "-")[:24], SOURCE_TEXT.get(e["source"], e["source"]),
                     who[:30], e["hash"][:12]))
    doc.table(["Date", "Site", "Recorded via", "Vouched for by", "Reference"], rows, [24, 46, 28, 48, 28])

    doc.h2("E. How this record was kept")
    _integrity(doc, chain, summary.cancelled_entries)
    doc.para(("Employer-side confirmations: " + (f"all {attestations.checked} are intact and linked to the entries "
              "they confirm." if attestations.ok else f"WARNING: {attestations.reason}.")) if attestations.checked
             else "No employer-side confirmations yet.")

    doc.h2("F. For office use")
    doc.para("Documents checked against originals:  [ ] Identity   [ ] Age   [ ] Bank   [ ] Photo\n"
             "Work days accepted: ________     Registration no.: ______________________\n\n"
             "Officer name, signature and date: ______________________________________")
    doc.ln(4)
    doc.para("Signature / thumb impression of worker: ____________________")
    doc.output(path)
