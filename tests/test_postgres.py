"""Postgres leg of the tenancy suite (plans/v2/04 E5).

Runs ONLY when LEADGEN_TEST_DATABASE_URL points at a DISPOSABLE Postgres
database — the schema is dropped and recreated on every run. Without the
variable the whole file skips (SQLite remains the zero-config default for
local dev; the owner runs this once against a scratch Render/Docker Postgres
before flipping DATABASE_URL in production).

    LEADGEN_TEST_DATABASE_URL=postgres://.../leadgen_test \
        python -m unittest tests.test_postgres -v
"""
from __future__ import annotations

import os
import unittest

try:
    from tests.tenancy import TenancyTestMixin  # puts tools/ on sys.path
except ImportError:
    from tenancy import TenancyTestMixin

import db
import lead_db

PG_URL = os.environ.get("LEADGEN_TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(PG_URL, "LEADGEN_TEST_DATABASE_URL not set")
class PostgresTenancyTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        # Wipe the DISPOSABLE test database, then route everything at it.
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as con:
            con.execute("DROP SCHEMA public CASCADE")
            con.execute("CREATE SCHEMA public")
        db.reset_pool()  # the rebuilt database must be migrated again
        lead_db._offer_seeded.clear()
        os.environ["DATABASE_URL"] = PG_URL

    def tearDown(self) -> None:
        db.reset_pool()
        self.teardown_tenancy()

    def test_migrations_and_core_crud(self):
        con = lead_db.connect()  # runs every migration on Postgres
        try:
            self.assertEqual(con.dialect, "postgres")
            names = [r["name"] for r in con.execute(
                "SELECT name FROM schema_migrations ORDER BY name").fetchall()]
            on_disk = sorted(p.name.split(".")[0] for p in
                             db.MIGRATIONS_DIR.glob("*.postgres.sql"))
            self.assertEqual(names, on_disk)
        finally:
            con.close()

        ws2 = lead_db.create_workspace("PG Second")
        biz = {"name": "PG Biz", "place_id": "pg-1", "city": "Boise ID",
               "email": "pg@x.example", "email_verified": "mx_ok", "score": 5}
        self.assertEqual(lead_db.upsert_leads(1, [dict(biz)])["inserted"], 1)
        self.assertEqual(lead_db.upsert_leads(1, [dict(biz)])["updated"], 1)
        self.assertEqual(lead_db.upsert_leads(ws2, [dict(biz)])["inserted"], 1)
        self.assertEqual(len(lead_db.get_leads(1)), 1)
        self.assertEqual(len(lead_db.get_leads(ws2)), 1)

        lead = lead_db.get_leads(1)[0]
        lead_db.set_status(1, lead["id"], "contacted")
        self.assertEqual(lead_db.get_lead(1, lead["id"])["status"], "contacted")
        self.assertEqual(lead_db.count_by_status(1), {"contacted": 1})

        # Suppression tiers.
        lead_db.add_suppression(1, "pg@x.example", "not_interested")
        self.assertTrue(lead_db.is_suppressed(1, "pg@x.example"))
        self.assertFalse(lead_db.is_suppressed(ws2, "pg@x.example"))
        lead_db.add_hard_suppression("pg@x.example", "unsubscribed")
        self.assertTrue(lead_db.is_suppressed(ws2, "pg@x.example"))
        lead_db.add_hard_suppression("pg@x.example", "unsubscribed")  # idempotent

        # Offers per workspace (each got a seeded default).
        self.assertIsNotNone(lead_db.get_default_offer(1))
        self.assertIsNotNone(lead_db.get_default_offer(ws2))
        offer_id = lead_db.create_offer(1, {
            "name": "PG offer", "product_description": "d", "value_prop": "v"})
        lead_db.set_default_offer(1, offer_id)
        self.assertEqual(lead_db.get_default_offer(1)["id"], offer_id)
        # The other workspace's default is untouched.
        self.assertNotEqual(lead_db.get_default_offer(ws2)["id"], offer_id)

    def test_enroll_and_tick_on_postgres(self):
        from datetime import datetime, timezone
        from enroll_leads import create_campaign, enroll
        import sequencer

        lead_db.upsert_leads(1, [{
            "name": "PG Window", "place_id": "pgw-1", "city": "Boise ID",
            "email": "win@x.example", "email_verified": "mx_ok", "score": 5,
            "email_subject": "hi", "email_body": "hello",
        }])
        campaign_id = create_campaign(1, "PG Campaign", 30)
        self.assertEqual(enroll(1, campaign_id, min_score=4, limit=10)["enrolled"], 1)

        con = lead_db.connect()
        try:
            with con.tx():
                con.execute("UPDATE sends SET scheduled_at = ? "
                            "WHERE workspace_id = 1", ("2026-07-01 00:00:00",))
        finally:
            con.close()
        in_window = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
        counters = sequencer.tick(now_utc=in_window, spacing=None)
        self.assertEqual(counters["sent"], 1)  # DRY_RUN — nothing real sent


if __name__ == "__main__":
    unittest.main()
