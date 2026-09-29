"""Tests for the unified business/person lead schema in tools/lead_db.py
(V2 Stage 3, plans/v2/03 A).

Covers the person columns migration and the per-entity-type dedupe rules:
businesses keep the original key (place_id → website+name); persons dedupe
on email first, then (name, company). Stdlib unittest only; each test points
DB_PATH at a fresh temp file so it never touches data/leads.db.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import tools.lead_db as lead_db

PERSON = {
    "entity_type": "person",
    "name": "Jane Marsh",
    "company": "Marsh Marketing",
    "title": "Founder",
    "website": "https://marshmarketing.example",
    "linkedin_url": "https://www.linkedin.com/in/jane-marsh",
    "email": "jane@marshmarketing.example",
    "email_source": "api",
    "phone": "+1 512 555 0100",
    "city": "Austin",
    "source": "b2b_data_api",
    "source_id": "pdl_123",
    "source_raw": {"job_start_date": "2019-04"},
}

BUSINESS = {
    "place_id": "gm_1",
    "name": "Bright Smile Dental",
    "website": "https://brightsmile.example",
    "city": "Boise",
}


class PersonSchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        lead_db.DB_PATH = Path(self._tmpdir.name) / "leads.db"

    def tearDown(self) -> None:
        lead_db.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_person_columns_exist_and_roundtrip(self):
        counts = lead_db.upsert_leads(1, [dict(PERSON)])
        self.assertEqual(counts, {"inserted": 1, "updated": 0})
        lead = lead_db.get_leads(1)[0]
        self.assertEqual(lead["entity_type"], "person")
        self.assertEqual(lead["company"], "Marsh Marketing")
        self.assertEqual(lead["title"], "Founder")
        self.assertEqual(lead["linkedin_url"], PERSON["linkedin_url"])
        self.assertEqual(lead["source"], "b2b_data_api")
        self.assertEqual(lead["source_id"], "pdl_123")
        self.assertIn("job_start_date", lead["source_raw"])  # stored as JSON

    def test_migration_adds_columns_to_preexisting_db(self):
        # Simulate a pre-Stage-3 database: leads table without person columns.
        con = sqlite3.connect(lead_db.DB_PATH)
        con.execute(
            "CREATE TABLE leads (id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " website TEXT, email TEXT, status TEXT DEFAULT 'new')"
        )
        con.execute("INSERT INTO leads (name, website) VALUES ('Old Row', 'https://o.example')")
        con.commit()
        con.close()

        con = lead_db.connect()  # runs _migrate
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
            old = con.execute("SELECT * FROM leads WHERE name = 'Old Row'").fetchone()
        finally:
            con.close()
        for col in ("entity_type", "company", "title", "linkedin_url",
                    "source", "source_id", "source_raw"):
            self.assertIn(col, cols)
        self.assertIsNone(old["entity_type"])  # NULL = business, untouched

    def test_person_dedupes_on_email_first(self):
        lead_db.upsert_leads(1, [dict(PERSON)])
        # Same email, different name spelling — must update, not duplicate.
        again = {**PERSON, "name": "Jane R. Marsh", "company": "Marsh Mktg LLC"}
        counts = lead_db.upsert_leads(1, [again])
        self.assertEqual(counts, {"inserted": 0, "updated": 1})
        self.assertEqual(len(lead_db.get_leads(1)), 1)

    def test_person_dedupes_on_name_company_when_no_email(self):
        no_email = {**PERSON, "email": None}
        lead_db.upsert_leads(1, [dict(no_email)])
        counts = lead_db.upsert_leads(1, [dict(no_email)])
        self.assertEqual(counts, {"inserted": 0, "updated": 1})
        self.assertEqual(len(lead_db.get_leads(1)), 1)

    def test_person_without_email_or_company_inserts_fresh(self):
        bare = {"entity_type": "person", "name": "Solo Person"}
        lead_db.upsert_leads(1, [dict(bare)])
        counts = lead_db.upsert_leads(1, [dict(bare)])
        # No dedupe key at all — two rows is the honest outcome.
        self.assertEqual(counts, {"inserted": 1, "updated": 0})

    def test_person_email_never_matches_business_row(self):
        business = {**BUSINESS, "email": "hello@brightsmile.example"}
        lead_db.upsert_leads(1, [business])
        person = {**PERSON, "email": "hello@brightsmile.example",
                  "name": "Someone Else", "company": "Elsewhere Inc"}
        counts = lead_db.upsert_leads(1, [person])
        self.assertEqual(counts, {"inserted": 1, "updated": 0})
        self.assertEqual(len(lead_db.get_leads(1)), 2)

    def test_person_never_absorbs_business_with_same_website_and_name(self):
        lead_db.upsert_leads(1, [dict(BUSINESS)])
        person = {"entity_type": "person", "name": BUSINESS["name"],
                  "website": BUSINESS["website"]}
        counts = lead_db.upsert_leads(1, [person])
        self.assertEqual(counts, {"inserted": 1, "updated": 0})

    def test_business_dedupe_unchanged(self):
        lead_db.upsert_leads(1, [dict(BUSINESS)])
        counts = lead_db.upsert_leads(1, [dict(BUSINESS)])
        self.assertEqual(counts, {"inserted": 0, "updated": 1})
        # Fallback key (website+name, no place_id) still works too.
        no_pid = {k: v for k, v in BUSINESS.items() if k != "place_id"}
        counts = lead_db.upsert_leads(1, [no_pid])
        self.assertEqual(counts, {"inserted": 0, "updated": 1})

    def test_person_status_progress_preserved_on_refresh(self):
        lead_db.upsert_leads(1, [dict(PERSON)])
        lead = lead_db.get_leads(1)[0]
        lead_db.set_status(1, lead["id"], "contacted")
        lead_db.upsert_leads(1, [dict(PERSON)])  # fresh pull of the same person
        self.assertEqual(lead_db.get_leads(1)[0]["status"], "contacted")


if __name__ == "__main__":
    unittest.main()
