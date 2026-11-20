"""Fill the database with sample sites and workers so the website has something to show.

Usage: python -m scripts.seed_demo   (uses KP_DB_PATH, default data/kaampramaan.db)
All names and numbers here are made up for the demo.
"""
import random
from datetime import timedelta

from app import attest, ledger
from app.config import load_settings
from app.db import Database

random.seed(7)
TODAY = load_settings().today()


def work_days(n_days, skip_sundays=True):
    d, out = TODAY - timedelta(days=int(n_days * 1.3)), []
    while d <= TODAY and len(out) < n_days:
        if not (skip_sundays and d.weekday() == 6):
            out.append(d)
        d += timedelta(days=1)
    return out


def main():
    db = Database(load_settings().db_path)
    pwd = db.create_site(project="Road widening, Sector 62", principal_employer="PWD Delhi",
                         employer_type="government", contractor="Ramesh Constructions", location="Delhi",
                         supervisor_name="Mohan Singh", supervisor_phone="919800000201")
    tower = db.create_site(project="Green Heights Tower B", principal_employer="Skyline Developers Pvt Ltd",
                           employer_type="developer", contractor="Verma Infra", location="Noida",
                           supervisor_name="Kiran Patel", supervisor_phone="919800000202")

    # phone, name, village, site, contractor, wage, days, share of weeks paid, share of days the supervisor confirmed
    people = [
        ("919900000101", "Raju Kumar", "Bhojpur", pwd, "Ramesh", 700, 110, 0.55, 1.0),     # ready to register
        ("919900000102", "Shankar Paswan", "Gaya", pwd, "Ramesh", 700, 95, 0.75, 0.7),     # needs confirmation
        ("919900000103", "Asha Devi", "Purnia", tower, "Verma", 650, 75, 0.95, 1.0),       # close
        ("919900000104", "Imran Ali", "Malda", tower, "Verma", 750, 130, 1.0, 1.0),        # registered, renewal due
        ("919900000105", "Sunita Kumari", "Ara", tower, "Verma", 650, 40, 1.0, 0.5),
    ]
    for phone, name, village, site, contractor, wage, n, paid_share, confirmed_share in people:
        if db.get_worker_by_phone(phone):
            print(f"{name} already exists, skipping")
            continue
        wid = db.create_worker(phone, name=name, village=village, state="active", site_id=site)
        db.update_worker(wid, last_wage=wage)
        confirmed = []
        for i, d in enumerate(work_days(n)):
            e = ledger.append(db, ledger.NewEntry(
                worker_id=wid, kind="work", work_date=d.isoformat(), amount=wage, source="site_qr",
                site_id=site, contractor=contractor, paid=False,
                transcript=f"Checked in by QR; Aaj {contractor} ke saath kaam kiya, {wage}",
            ))
            if random.random() < confirmed_share:
                confirmed.append(e)
            if i % 6 == 5 and random.random() < paid_share:  # weekly payment, sometimes skipped
                ledger.append(db, ledger.NewEntry(
                    worker_id=wid, kind="payment", work_date=d.isoformat(), amount=wage * 6,
                    source="whatsapp", site_id=site, contractor=contractor,
                    transcript=f"{contractor} ne {wage * 6} diye",
                ))
        supervisor = "Mohan Singh (919800000201)" if site == pwd else "Kiran Patel (919800000202)"
        attest.attest(db, confirmed, "supervisor", supervisor)
        print(f"added {name}: {n} days, {len(confirmed)} confirmed by the supervisor")
    imran = db.get_worker_by_phone("919900000104")
    db.update_worker(imran["id"], board_reg_no="UP/BOCW/2025/48213",
                     board_reg_date=(TODAY - timedelta(days=350)).isoformat())

    # Two workers without phones, logged from the site's paper register
    for name in ("Gopal Ram", "Lakshmi Bai"):
        wid = db.create_worker(None, name=name, village="Barabanki", state="active", site_id=pwd)
        made = [ledger.append(db, ledger.NewEntry(worker_id=wid, kind="work", work_date=d.isoformat(), amount=0,
                                                  source="muster_photo", site_id=pwd, contractor="Ramesh",
                                                  transcript="Present in muster register (sample)"))
                for d in work_days(30)]
        attest.attest(db, made, "muster_register", "Muster register (sample data)")
    print("added 2 workers from the paper muster register")

    openings = [
        ("Plaster work, interior walls", "Verma Infra", "Sector 75, Noida", (28.5770, 77.3850), 750),
        ("Shuttering carpenter for slab casting", "Ramesh Constructions", "Sector 63, Noida", (28.6280, 77.3800), 850),
        ("Helper for loading and unloading material", "Verma Infra", "Sector 62, Noida", (28.6270, 77.3720), 550),
        ("Painting work, 2 painters needed", "Mohan", "Indirapuram", (28.6410, 77.3710), 700),
    ]
    for title, contractor, place, (lat, lon), wage in openings:
        db.create_opening(title=title, wage=wage, starts_on=TODAY.isoformat(), posted_by="demo",
                          contractor=contractor, location=place, latitude=lat, longitude=lon,
                          slots=random.randint(2, 6), contact="9800000000")
    print(f"added {len(openings)} work openings")
    db.create_case("death_report", reporter_phone="919900000999",
                   details="Mere bhai Mukesh ki Sector 62 site pe accident mein maut ho gayi")


if __name__ == "__main__":
    main()
