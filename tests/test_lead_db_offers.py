"""Tests for the offer_profiles table and helpers in tools/lead_db.py.

Stdlib unittest only (no pytest dependency). Each test points DB_PATH at a
fresh temp file so it never touches data/leads.db.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import tools.lead_db as lead_db


class OfferProfilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        lead_db.DB_PATH = Path(self._tmpdir.name) / "leads.db"

    def tearDown(self) -> None:
        lead_db.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_table_created_and_seeded(self):
        con = lead_db.connect()
        try:
            rows = con.execute(
                "SELECT * FROM offer_profiles WHERE deleted = 0"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        seed = dict(rows[0])
        self.assertEqual(seed["name"], "SMS text-back bot")
        self.assertEqual(seed["is_default"], 1)
        self.assertTrue(seed["product_description"])
        self.assertTrue(seed["value_prop"])

    def test_seed_is_idempotent(self):
        lead_db.connect().close()
        lead_db.connect().close()
        offers = lead_db.list_offers(1, include_deleted=True)
        self.assertEqual(len(offers), 1)

    def test_create_get_list_update(self):
        offer_id = lead_db.create_offer(1, {
            "name": "Booking widget",
            "product_description": "A booking widget for websites.",
            "value_prop": "Fewer no-shows.",
            "target_customer": "Salons",
        })
        fetched = lead_db.get_offer(1, offer_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["name"], "Booking widget")

        all_offers = lead_db.list_offers(1)
        names = {o["name"] for o in all_offers}
        self.assertIn("Booking widget", names)
        self.assertIn("SMS text-back bot", names)

        lead_db.update_offer(1, offer_id, {"name": "Booking widget v2", "is_default": 1})
        updated = lead_db.get_offer(1, offer_id)
        self.assertEqual(updated["name"], "Booking widget v2")
        # is_default is not a whitelisted column for update_offer
        self.assertEqual(updated["is_default"], 0)

    def test_create_offer_requires_core_fields(self):
        with self.assertRaises(ValueError):
            lead_db.create_offer(1, {"name": "", "product_description": "x", "value_prop": "y"})
        with self.assertRaises(ValueError):
            lead_db.create_offer(1, {"name": "x", "product_description": "", "value_prop": "y"})
        with self.assertRaises(ValueError):
            lead_db.create_offer(1, {"name": "x", "product_description": "y", "value_prop": ""})

    def test_soft_delete_hides_but_get_offer_still_returns(self):
        offer_id = lead_db.create_offer(1, {
            "name": "Extra offer",
            "product_description": "desc",
            "value_prop": "value",
        })
        lead_db.delete_offer(1, offer_id)

        active = lead_db.list_offers(1)
        self.assertNotIn(offer_id, [o["id"] for o in active])

        all_offers = lead_db.list_offers(1, include_deleted=True)
        self.assertIn(offer_id, [o["id"] for o in all_offers])

        fetched = lead_db.get_offer(1, offer_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["deleted"], 1)

    def test_delete_default_offer_raises(self):
        default_offer = lead_db.get_default_offer(1)
        self.assertIsNotNone(default_offer)
        with self.assertRaises(ValueError):
            lead_db.delete_offer(1, default_offer["id"])
        # still not deleted
        still_there = lead_db.get_offer(1, default_offer["id"])
        self.assertEqual(still_there["deleted"], 0)

    def test_set_default_offer_swaps_default(self):
        original_default = lead_db.get_default_offer(1)
        new_id = lead_db.create_offer(1, {
            "name": "New default offer",
            "product_description": "desc",
            "value_prop": "value",
        })
        lead_db.set_default_offer(1, new_id)

        current_default = lead_db.get_default_offer(1)
        self.assertEqual(current_default["id"], new_id)

        old = lead_db.get_offer(1, original_default["id"])
        self.assertEqual(old["is_default"], 0)

    def test_set_default_offer_refuses_deleted(self):
        offer_id = lead_db.create_offer(1, {
            "name": "Doomed offer",
            "product_description": "desc",
            "value_prop": "value",
        })
        lead_db.delete_offer(1, offer_id)
        with self.assertRaises(ValueError):
            lead_db.set_default_offer(1, offer_id)

    def test_upsert_lead_persists_offer_id(self):
        offer_id = lead_db.create_offer(1, {
            "name": "Widget offer",
            "product_description": "desc",
            "value_prop": "value",
        })
        con = lead_db.connect()
        try:
            with con.tx():
                lead_id, action = lead_db.upsert_lead(
                    con, 1, {"name": "Acme Dental", "offer_id": offer_id}
                )
            self.assertEqual(action, "inserted")
        finally:
            con.close()

        lead = lead_db.get_lead(1, lead_id)
        self.assertEqual(lead["offer_id"], offer_id)

    def test_upsert_lead_without_offer_id_still_works(self):
        con = lead_db.connect()
        try:
            with con.tx():
                lead_id, action = lead_db.upsert_lead(con, 1, {"name": "No Offer Biz"})
            self.assertEqual(action, "inserted")
        finally:
            con.close()

        lead = lead_db.get_lead(1, lead_id)
        self.assertIsNone(lead["offer_id"])

    def test_migration_adds_offer_id_to_existing_db(self):
        # Simulate a DB that predates offer_id (and the migration runner):
        # a legacy leads/campaigns table without it. connect() must patch the
        # schema up to baseline and then apply the tenancy migration.
        con = sqlite3.connect(lead_db.DB_PATH)
        con.executescript(
            "CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL, website TEXT, email TEXT,"
            " status TEXT DEFAULT 'new');"
            "CREATE TABLE campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL, status TEXT DEFAULT 'draft');"
        )
        con.commit()
        con.close()

        con = lead_db.connect()
        try:
            leads_cols = {r[1] for r in con.raw.execute("PRAGMA table_info(leads)")}
            campaigns_cols = {r[1] for r in con.raw.execute("PRAGMA table_info(campaigns)")}
        finally:
            con.close()
        self.assertIn("offer_id", leads_cols)
        self.assertIn("offer_id", campaigns_cols)
        self.assertIn("workspace_id", leads_cols)
        self.assertIn("workspace_id", campaigns_cols)


if __name__ == "__main__":
    unittest.main()
