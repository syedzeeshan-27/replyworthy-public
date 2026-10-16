"""Fill the database with made-up sample data, so the dashboard has something
to show before you add any keys (and so the public demo has a story).

    python tools/seed_demo.py            # seed an empty local install
    python tools/seed_demo.py --reset    # wipe the local SQLite file first

Everything here is fake: businesses, people and addresses use example.com and
555 numbers. Nothing is sent and no paid service is called. The app runs this
itself on every boot when DEMO_MODE=true (web/app.py), so the public demo
resets to a clean state whenever the server restarts.

SQLite only: it refuses to touch a Postgres database (DATABASE_URL set),
because a demo wipe there would be someone's real data.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import lead_db  # noqa: E402
from enroll_leads import create_campaign  # noqa: E402

OWNER_WS = lead_db.OWNER_WORKSPACE_ID

# (name, category, area, contact first name, rating, reviews, score, status)
BUSINESSES = [
    ("Cedar Park Family Dental", "dentist", "Cedar Park", "Priya", 4.8, 212, 5, "interested"),
    ("Barton Creek Smiles", "dentist", "Barton Creek", "Marcus", 4.6, 148, 5, "meeting"),
    ("Hyde Park Dental Studio", "dentist", "Hyde Park", "Elena", 4.9, 96, 5, "needs_human"),
    ("Mueller Kids Dentistry", "dentist", "Mueller", "Jordan", 4.7, 301, 4, "contacted"),
    ("Zilker Orthodontics", "orthodontist", "Zilker", "Sam", 4.9, 187, 5, "won"),
    ("South Lamar Dental Care", "dentist", "South Lamar", "Nadia", 4.4, 77, 4, "contacted"),
    ("Round Rock Gentle Dental", "dentist", "Round Rock", "Chris", 4.5, 260, 4, "contacted"),
    ("Westlake Cosmetic Dentistry", "dentist", "Westlake", "Olivia", 4.8, 133, 5, "needs_human"),
    ("Pflugerville Dental Group", "dentist", "Pflugerville", "Diego", 4.3, 58, 3, "queued"),
    ("East Austin Dental Co.", "dentist", "East Austin", "Hana", 4.6, 112, 4, "not_interested"),
    ("Glow Med Spa Austin", "med spa", "Downtown", "Taylor", 4.7, 389, 5, "interested"),
    ("Lumen Aesthetics", "med spa", "Domain", "Ava", 4.8, 201, 5, "contacted"),
    ("Restore Skin Clinic", "med spa", "Tarrytown", "Mia", 4.5, 64, 4, "contacted"),
    ("Bluebonnet Laser & Skin", "med spa", "Circle C", "Grace", 4.2, 41, 3, "new"),
    ("Riverside Chiropractic", "chiropractor", "Riverside", "Ben", 4.9, 156, 4, "contacted"),
    ("Align Spine Center", "chiropractor", "North Loop", "Ethan", 4.6, 88, 4, "queued"),
    ("Spicewood Chiropractic", "chiropractor", "Spicewood", "Leah", 4.4, 39, 3, "new"),
    ("Core Motion Physio", "physical therapy", "Allandale", "Noah", 4.8, 120, 4, "contacted"),
    ("Studio 6 Hair Lounge", "hair salon", "Rainey St", "Zoe", 4.7, 455, 4, "contacted"),
    ("The Mane Room", "hair salon", "Bouldin", "Lily", 4.6, 230, 4, "unsubscribed"),
    ("Brow & Lash Bar ATX", "beauty salon", "Crestview", "Ivy", 4.9, 512, 5, "interested"),
    ("Driftwood Barbers", "barber", "Holly", "Luis", 4.8, 340, 3, "new"),
    ("Paws on Lamar Grooming", "pet groomer", "South Lamar", "Kate", 4.7, 198, 3, "queued"),
    ("Hill Country Vet Clinic", "veterinarian", "Oak Hill", "Ryan", 4.6, 276, 4, "contacted"),
    ("Live Oak Massage", "massage", "Brentwood", "Ruby", 4.9, 167, 4, "lost"),
    ("Trailhead Yoga Studio", "yoga studio", "Zilker", "Maya", 4.8, 143, 3, "new"),
    ("Iron Republic Gym", "gym", "Govalle", "Tyler", 4.5, 402, 2, "new"),
    ("Clearview Eye Care", "optometrist", "Anderson Mill", "Rosa", 4.7, 189, 4, "contacted"),
    ("Pecan Street Pediatrics", "pediatrician", "Georgetown", "Amir", 4.6, 95, 4, "queued"),
    ("Sunset Valley Smiles", "dentist", "Sunset Valley", "Claire", 4.1, 33, 2, "new"),
    ("Lakeway Implant Center", "dentist", "Lakeway", "Victor", 4.8, 101, 5, "contacted"),
    ("Brushy Creek Dental", "dentist", "Brushy Creek", "Jen", 4.5, 72, 3, "new"),
    ("Bee Cave Family Dentistry", "dentist", "Bee Cave", "Owen", 4.7, 158, 4, "queued"),
    ("North Austin Hearing", "audiologist", "Wells Branch", "Paula", 4.3, 46, 3, "new"),
    ("Manor Road Tattoo", "tattoo studio", "Cherrywood", "Kai", 4.9, 610, 1, "excluded"),
    ("Red River Nail Lounge", "nail salon", "Red River", "Linh", 4.4, 287, 3, "new"),
]

PAINS = {
    "booking": "No online booking: new patients have to call during office hours",
    "after_hours": "Phone goes to voicemail after 5pm and on weekends",
    "reviews": "Strong reviews, but nothing on the site turns visitors into bookings",
    "mobile": "Contact form is hard to use on a phone",
    "reminders": "Several reviews mention long waits to get a call back",
}

REPLIES = {
    "interested": ("Re: quick idea for {name}",
                   "Hi, this is actually timely. We lose a lot of calls after hours. "
                   "Can you send over a couple of times next week?",
                   "Wants a call next week; asked for times.",
                   "Reply with two time slots."),
    "meeting": ("Re: quick idea for {name}",
                "Sure, Thursday at 2 works. Call the front desk line and ask for me.",
                "Agreed to Thursday 2pm.",
                "Confirm the meeting and add it to the calendar."),
    "needs_human": ("Re: quick idea for {name}",
                    "What does this cost, and does it work with the software we already use for charts?",
                    "Asking about price and compatibility.",
                    "Answer pricing and integration questions personally."),
    "not_interested": ("Re: quick idea for {name}",
                       "Thanks, but we're all set for now.",
                       "Not interested right now.",
                       "Sequence stopped. Revisit in 6 months."),
    "unsubscribed": ("Re: quick idea for {name}",
                     "Please remove me from your list.",
                     "Asked to be removed.",
                     "Unsubscribed and suppressed everywhere."),
    "won": ("Re: quick idea for {name}",
            "Signed the agreement. Looking forward to getting this live.",
            "Closed: signed.", "Kick off onboarding."),
    "lost": ("Re: quick idea for {name}",
             "We decided to go with someone local we already know. Thanks anyway.",
             "Chose another provider.", "Mark lost."),
}

SENT_STATUSES = ("contacted", "needs_human", "interested", "meeting", "won",
                 "lost", "not_interested", "unsubscribed")


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _slug(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())[:24]


def _email_copy(name: str, first: str, category: str, rating: float,
                reviews: int, pain: str) -> tuple[str, str]:
    subject = f"quick idea for {name}"
    body = (
        f"Hi {first},\n\n"
        f"{name} has {reviews} reviews at {rating} stars, which is better than most "
        f"{category}s in Austin. One thing stood out on your site: {pain.lower()}.\n\n"
        "We set up a booking link plus an after-hours text-back, so people who find "
        "you at 9pm can book right then instead of calling tomorrow (most don't).\n\n"
        "Worth a 10 minute look? If not, no worries at all.\n\n"
        "Alex\nNorthside Growth Co."
    )
    return subject, body


def _leads() -> list[dict]:
    rng = random.Random(7)
    out = []
    for i, (name, category, area, first, rating, reviews, score, _status) in enumerate(BUSINESSES):
        slug = _slug(name)
        pains = rng.sample(list(PAINS.values()), 2 if score >= 4 else 1)
        subject, body = _email_copy(name, first, category, rating, reviews, pains[0])
        lead = {
            "place_id": f"demo-{slug}",
            "name": name, "category": category, "city": "Austin TX", "area": area,
            "website": f"https://{slug}.example.com",
            "phone": f"(512) 555-{1000 + i * 37:04d}",
            "email": f"{first.lower()}@{slug}.example.com",
            "email_source": "website", "email_verified": "mx_ok",
            "rating": rating, "reviews_count": reviews, "score": score,
            "pain_points": pains,
            "soft_signals": ["'Book now' button opens a phone number"] if score >= 4 else [],
            "email_subject": subject if score >= 3 else None,
            "email_body": body if score >= 3 else None,
            "source": "google_maps",
        }
        dm_only = _status == "new" and i % 3 == 1  # no public email: DM them
        if i % 4 == 0 or dm_only:
            lead["social_links"] = {"instagram": f"https://instagram.com/{slug}",
                                    "facebook": f"https://facebook.com/{slug}"}
        if dm_only:
            lead["email"] = None
        out.append(lead)
    return out


def seed() -> dict:
    """Seed workspace 1 (plus a second workspace for the admin switcher)."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    lead_db.rename_workspace(OWNER_WS, "Northside Growth Co.")
    lead_db.set_workspace_identity(OWNER_WS, "100 Example St, Austin TX 78701", "")

    offer_id = lead_db.create_offer(OWNER_WS, {
        "name": "Online booking + after-hours text-back",
        "product_description": "A booking link on the client's site and an automatic "
                               "text-back for missed calls, set up and managed for them.",
        "value_prop": "Turn after-hours visitors and missed calls into booked appointments",
        "target_customer": "Appointment-based local businesses with good reviews",
        "tone": "direct, friendly", "sender_name": "Alex", "sender_title": "Founder",
    })
    lead_db.set_default_offer(OWNER_WS, offer_id)
    lead_db.create_offer(OWNER_WS, {
        "name": "Review booster",
        "product_description": "Automatic review requests by text after each visit.",
        "value_prop": "More recent 5-star reviews without asking at the front desk",
        "target_customer": "Clinics and salons with under 100 reviews",
    })

    leads = _leads()
    lead_db.upsert_leads(OWNER_WS, leads)
    by_place = {l["place_id"]: l for l in lead_db.get_leads(OWNER_WS)}

    dental = create_campaign(OWNER_WS, "Austin dentists: booking link", 30, offer_id)
    beauty = create_campaign(OWNER_WS, "Med spas and salons", 25, offer_id)

    con = lead_db.connect()
    counts = {"leads": len(leads), "sends": 0, "replies": 0}
    try:
        with con.tx():
            for i, biz in enumerate(BUSINESSES):
                name, category, _area, _first, _r, _n, _score, status = biz
                lead = by_place[f"demo-{_slug(name)}"]
                campaign = dental if category in ("dentist", "orthodontist") else beauty
                mailbox = "alex@northside-growth.example" if i % 2 else "sam@northside-growth.example"
                first_send = now - timedelta(days=12 - (i % 9), hours=i % 7)
                if status in SENT_STATUSES and lead["email"]:
                    steps = 3 if status == "contacted" and i % 3 == 0 else (2 if status == "contacted" else 1)
                    for step in range(1, 4):
                        at = first_send + timedelta(days=(step - 1) * 3)
                        sent = step <= steps and at < now
                        if not sent and status == "contacted" and at <= now:  # still to come
                            at = now + timedelta(hours=3 + i % 30)
                        con.execute(
                            "INSERT INTO sends (workspace_id, lead_id, campaign_id, step_number, "
                            "mailbox, message_id, scheduled_at, sent_at, status) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (OWNER_WS, lead["id"], campaign, step, mailbox,
                             f"<demo-{lead['id']}-{step}@northside-growth.example>" if sent else None,
                             _ts(at), _ts(at) if sent else None,
                             "sent" if sent else ("scheduled" if status == "contacted" else "cancelled")),
                        )
                        counts["sends"] += 1
                elif status == "queued" and lead["email"]:
                    con.execute(
                        "INSERT INTO sends (workspace_id, lead_id, campaign_id, step_number, "
                        "mailbox, scheduled_at, status) VALUES (?, ?, ?, 1, ?, ?, 'scheduled')",
                        (OWNER_WS, lead["id"], campaign, mailbox,
                         _ts(now + timedelta(hours=2 + i % 20))),
                    )
                    counts["sends"] += 1

                if status in REPLIES and lead["email"]:
                    subject, text, summary, action = REPLIES[status]
                    got = first_send + timedelta(days=1, hours=3)
                    classification = {"meeting": "interested", "won": "interested",
                                      "lost": "not_interested",
                                      "unsubscribed": "unsubscribe"}.get(status, status)
                    con.execute(
                        "INSERT INTO replies (workspace_id, lead_id, mailbox, from_email, "
                        "subject, body_text, received_at, imap_uid, classification, "
                        "confidence, summary, suggested_action, processed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'high', ?, ?, ?)",
                        (OWNER_WS, lead["id"], mailbox, lead["email"],
                         subject.format(name=name), text, _ts(got),
                         f"INBOX:demo-{lead['id']}", classification, summary, action,
                         _ts(got + timedelta(minutes=4))),
                    )
                    counts["replies"] += 1
                    if status in ("interested", "needs_human", "meeting"):
                        con.execute(
                            "INSERT INTO alerts (workspace_id, lead_id, kind, severity, title, "
                            "body, channel, created_at) VALUES (?, ?, ?, ?, ?, ?, 'stderr', ?)",
                            (OWNER_WS, lead["id"], status,
                             "urgent" if status != "meeting" else "info",
                             f"{name} replied: {summary}", text,
                             _ts(got + timedelta(minutes=5))),
                        )
                if status == "unsubscribed":
                    con.execute(
                        "INSERT INTO hard_suppression (email, reason, added_at) "
                        "VALUES (?, 'unsubscribed', ?) ON CONFLICT DO NOTHING",
                        (lead["email"], _ts(now - timedelta(days=4))),
                    )

            for address, age in (("alex@northside-growth.example", 34),
                                 ("sam@northside-growth.example", 9)):
                con.execute(
                    "INSERT INTO mailboxes (workspace_id, address, smtp_host, smtp_port, "
                    "imap_host, imap_port, creds_encrypted, daily_cap, warmup_started_at, "
                    "status, created_at) VALUES (?, ?, 'smtp.example.com', 465, "
                    "'imap.example.com', 993, 'demo-not-a-real-secret', 40, ?, 'active', ?)",
                    (OWNER_WS, address, _ts(now - timedelta(days=age)),
                     _ts(now - timedelta(days=age))),
                )
            con.execute(
                "INSERT INTO alerts (workspace_id, kind, severity, title, body, channel, "
                "created_at) VALUES (?, 'warmup', 'info', ?, ?, 'stderr', ?)",
                (OWNER_WS, "sam@ is on day 9 of warmup",
                 "Daily cap is ramping up automatically. No action needed.",
                 _ts(now - timedelta(hours=6))),
            )
    finally:
        con.close()

    for name, note in (("Cedar Park Family Dental", "Office manager is Priya. Prefers texts over calls."),
                       ("Barton Creek Smiles", "Demo booked for Thursday 2pm."),
                       ("Hyde Park Dental Studio", "Asked about price. Send the one-pager.")):
        lead = by_place[f"demo-{_slug(name)}"]
        lead_db.add_note(OWNER_WS, lead["id"], note)
    for biz in BUSINESSES:
        lead = by_place[f"demo-{_slug(biz[0])}"]
        if biz[7] not in ("new",):
            lead_db.set_status(OWNER_WS, lead["id"], biz[7])

    # A second agency, so the owner's workspace switcher has something to show.
    other = lead_db.create_workspace("Bright Smile Marketing")
    lead_db.upsert_leads(other, [{
        "place_id": "demo-other-1", "name": "Lone Star Dental Spa", "category": "dentist",
        "city": "Dallas TX", "email": "hello@lonestardental.example.com", "score": 4,
        "rating": 4.6, "reviews_count": 140,
    }])
    counts["workspaces"] = 2
    return counts


def reset_and_seed() -> dict:
    """Delete the local SQLite database and seed a fresh one (demo boot)."""
    if os.environ.get("DATABASE_URL", "").strip():
        raise SystemExit("seed_demo: DATABASE_URL is set; refusing to wipe a Postgres database.")
    for suffix in ("", "-wal", "-shm"):
        Path(str(lead_db.DB_PATH) + suffix).unlink(missing_ok=True)
    return seed()


def main() -> int:
    parser = argparse.ArgumentParser(description="Load made-up sample data.")
    parser.add_argument("--reset", action="store_true",
                        help="delete the local SQLite database first")
    args = parser.parse_args()
    if args.reset:
        print(reset_and_seed())
        return 0
    if os.environ.get("DATABASE_URL", "").strip():
        print("DATABASE_URL is set: sample data only goes into a local SQLite install.")
        return 1
    if lead_db.get_leads(OWNER_WS):
        print("Workspace 1 already has leads. Use --reset to start over with sample data.")
        return 1
    print(seed())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
