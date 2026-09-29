"""The Sending page (web/templates/campaigns.html).

The page used to say "3 emails waiting to go out" and nothing else; the
outbox card now names each pending email, so these tests pin what it shows
and when. The admin-only "Last run" gate is here too: TICK is a single
process-global dict whose counts span every workspace, so a tenant must
never see it.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import web.app as webapp
import web.jobs as jobs
from enroll_leads import create_campaign, enroll

try:
    from tests.tenancy import TenancyTestMixin, TENANT, signup_owner, signup_tenant
except ImportError:
    from tenancy import TenancyTestMixin, TENANT, signup_owner, signup_tenant

lead_db = webapp.lead_db


class CampaignsPageTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.owner = TestClient(webapp.app)
        signup_owner(self.owner)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.ws = lead_db.get_user_by_email(TENANT["email"])["workspace_id"]
        self._tick_backup = dict(jobs.TICK)

    def tearDown(self) -> None:
        jobs.TICK.clear()
        jobs.TICK.update(self._tick_backup)
        self.teardown_tenancy()

    def _seed(self, n: int = 1) -> int:
        leads = [{
            "name": f"Nav Dental {i}", "place_id": f"nav-{i}",
            "city": "Boise ID", "email": f"owner{i}@navdental.example",
            "email_verified": "mx_ok", "score": 5,
            "email_subject": "hi", "email_body": "body",
        } for i in range(1, n + 1)]
        lead_db.upsert_leads(self.ws, leads)
        campaign_id = create_campaign(self.ws, "July dentists", 30)
        enroll(self.ws, campaign_id, min_score=4, limit=100)
        return campaign_id

    def _pin(self, scheduled_at: str) -> None:
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE sends SET scheduled_at = ? WHERE workspace_id = ?",
                    (scheduled_at, self.ws))
        finally:
            con.close()

    def test_outbox_names_each_pending_email(self):
        self._seed()
        self._pin("2030-01-02 09:30:00")
        page = self.tenant.get("/campaigns").text
        self.assertIn("Waiting to send (1)", page)
        self.assertIn("Nav Dental 1", page)
        self.assertIn("owner1@navdental.example", page)
        self.assertIn("July dentists", page)
        self.assertIn("step 1", page)

    def test_outbox_rows_link_to_the_lead(self):
        self._seed()
        lead_id = lead_db.get_leads(self.ws)[0]["id"]
        page = self.tenant.get("/campaigns").text
        self.assertIn(f'class="ob-row" href="/lead/{lead_id}"', page)

    def test_future_send_shows_trimmed_utc_time(self):
        self._seed()
        self._pin("2030-01-02 09:30:00")
        page = self.tenant.get("/campaigns").text
        self.assertIn("2030-01-02 09:30 UTC", page)
        self.assertNotIn("2030-01-02 09:30:00", page)

    def test_due_send_explains_the_wait(self):
        self._seed()
        self._pin("2020-01-02 09:30:00")
        page = self.tenant.get("/campaigns").text
        self.assertIn("due — waiting for send window", page)

    def test_outbox_caps_at_ten_with_a_more_line(self):
        self._seed(12)
        page = self.tenant.get("/campaigns").text
        self.assertEqual(page.count('class="ob-row"'), 10)
        self.assertIn("Waiting to send (12)", page)
        self.assertIn("+2 more", page)

    def test_empty_outbox_state(self):
        page = self.tenant.get("/campaigns").text
        self.assertIn("Outbox is empty", page)
        self.assertNotIn("ob-row", page)

    def test_leads_by_status_card_is_gone(self):
        self._seed()
        page = self.tenant.get("/campaigns").text
        self.assertNotIn("Leads by status", page)
        self.assertIn("ready to enroll", page)

    def test_live_banner_is_gone(self):
        page = self.tenant.get("/campaigns").text
        self.assertNotIn("LIVE SENDING", page)

    def test_last_run_is_admin_only(self):
        jobs.TICK["running"] = False
        jobs.TICK["finished_at"] = datetime.now(timezone.utc)
        jobs.TICK["results"] = {"Emails": {"sent": 1}}
        self.assertNotIn("Last run", self.tenant.get("/campaigns").text)
        self.assertIn("Last run", self.owner.get("/campaigns").text)


if __name__ == "__main__":
    unittest.main()
