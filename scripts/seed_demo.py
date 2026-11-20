"""Fill the database with sample workers so the website has something to show.

Usage: python -m scripts.seed_demo   (uses KP_DB_PATH, default data/kaampramaan.db)
All names and numbers here are made up for the demo.
"""
import random
from datetime import timedelta

from app import ledger
from app.config import load_settings
from app.db import Database

random.seed(7)
TODAY = load_settings().today()


def work_days(n_days, skip_sundays=True):
    d, out = TODAY - timedelta(days=n_days * 1.3), []
    while d <= TODAY and len(out) < n_days:
        if not (skip_sundays and d.weekday() == 6):
            out.append(d)
        d += timedelta(days=1)
    return out


def main():
    db = Database(load_settings().db_path)
    pwd = db.create_site(project="Road widening, Sector 62", principal_employer="PWD Delhi",
                         employer_type="government", contractor="Ramesh Constructions", location="Delhi")
    tower = db.create_site(project="Green Heights Tower B", principal_employer="Skyline Developers Pvt Ltd",
                           employer_type="developer", contractor="Verma Infra", location="Noida")

    people = [
        ("919900000101", "Raju Kumar", "Bhojpur", pwd, "Ramesh", 700, 110, 0.55),
        ("919900000102", "Shankar Paswan", "Gaya", pwd, "Ramesh", 700, 95, 0.75),
        ("919900000103", "Asha Devi", "Purnia", tower, "Verma", 650, 60, 0.95),
        ("919900000104", "Imran Ali", "Malda", tower, "Verma", 750, 130, 1.0),
    ]
    for phone, name, village, site, contractor, wage, n, paid_share in people:
        if db.get_worker_by_phone(phone):
            print(f"{name} already exists, skipping")
            continue
        wid = db.create_worker(phone, name=name, village=village, state="active", site_id=site)
        db.update_worker(wid, last_wage=wage)
        days = work_days(n)
        for i, d in enumerate(days):
            ledger.append(db, ledger.NewEntry(
                worker_id=wid, kind="work", work_date=d.isoformat(), amount=wage, source="whatsapp",
                site_id=site, contractor=contractor, paid=False,
                transcript=f"Aaj {contractor} ke saath kaam kiya, {wage}",
            ))
            if i % 6 == 5 and random.random() < paid_share:  # weekly payment, sometimes skipped
                ledger.append(db, ledger.NewEntry(
                    worker_id=wid, kind="payment", work_date=d.isoformat(), amount=wage * 6,
                    source="whatsapp", site_id=site, contractor=contractor,
                    transcript=f"{contractor} ne {wage * 6} diye",
                ))
        print(f"added {name}: {len(days)} days")
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
