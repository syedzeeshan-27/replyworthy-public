"""Local single-user mode (no Supabase keys on plain http) and the public
demo (DEMO_MODE=true): who gets signed in, what a demo visitor can't do, and
that the demo never sends or pings anyone.

Run (from the repo root):
    python -m unittest tests.test_local_and_demo
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp

try:
    from tests.tenancy import TenancyTestMixin
except ImportError:  # unittest discover -s tests puts tests/ itself on sys.path
    from tenancy import TenancyTestMixin

import notify
import seed_demo
from send_email import dry_run_enabled

lead_db = webapp.lead_db

NO_SUPABASE = {"SUPABASE_URL": "", "SUPABASE_ANON_KEY": ""}


class LocalModeTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_no_keys_on_http_signs_you_in_as_the_owner(self):
        with mock.patch.dict(os.environ, NO_SUPABASE):
            client = TestClient(webapp.app)
            r = client.get("/", follow_redirects=False)
            self.assertEqual(r.status_code, 200)
            user = lead_db.get_user_by_email("owner@localhost")
            self.assertEqual((user["role"], user["workspace_id"]), ("admin", 1))
            # Same person on the next request (cookie), not a second account.
            client.get("/pipeline")
            self.assertEqual(lead_db.count_users(), 1)

    def test_no_keys_on_https_still_asks_for_login(self):
        with mock.patch.dict(os.environ, {**NO_SUPABASE,
                                          "BASE_URL": "https://app.example.com"}):
            r = TestClient(webapp.app).get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")
        self.assertEqual(lead_db.count_users(), 0)

    def test_with_keys_local_mode_is_off(self):
        r = TestClient(webapp.app).get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)


class DemoModeTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self._demo = mock.patch.dict(os.environ, {
            "DEMO_MODE": "true", "BASE_URL": "https://demo.example.com",
            "DRY_RUN": "false"})
        self._demo.start()
        seed_demo.seed()
        self.client = TestClient(webapp.app, base_url="https://testserver")

    def tearDown(self) -> None:
        self._demo.stop()
        self.teardown_tenancy()

    def test_visitor_lands_on_seeded_dashboard_with_banner(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Live demo", r.text)
        self.assertIn("Northside Growth Co.", r.text)
        self.assertGreater(len(lead_db.get_leads(1)), 30)

    def test_every_page_renders(self):
        for page in ("/", "/pipeline", "/queue", "/dms", "/campaigns",
                     "/mailboxes", "/offers", "/billing", "/lead/1"):
            self.assertEqual(self.client.get(page).status_code, 200, page)

    def test_paid_and_outbound_actions_are_refused(self):
        self.client.get("/")
        jobs_before = len(webapp.jobs_mod.JOBS)
        for path in ("/jobs", "/dms/1/write", "/mailboxes/create",
                     "/mailboxes/golive", "/lead/1/reply", "/campaigns/tick",
                     "/campaigns/pause", "/admin/billing/1/adjust"):
            r = self.client.post(path, data={}, follow_redirects=False,
                                 headers={"referer": "https://testserver/pipeline"})
            self.assertEqual(r.status_code, 303, path)
            self.assertTrue(r.headers["location"].startswith("/pipeline?msg="), path)
        self.assertEqual(len(webapp.jobs_mod.JOBS), jobs_before)  # no search started

    def test_harmless_edits_still_work(self):
        self.client.get("/")
        r = self.client.post("/lead/3/notes", data={"body": "demo note"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("switched+off", r.headers["location"])
        self.assertNotIn("switched%20off", r.headers["location"])

    def test_demo_never_sends_or_pings(self):
        self.assertTrue(dry_run_enabled())
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x",
                                          "TELEGRAM_CHAT_ID": "y",
                                          "ALERT_EMAIL": "a@b.example"}):
            with mock.patch("requests.post") as post:
                self.assertFalse(notify._telegram_send("hi"))
                post.assert_not_called()
            self.assertFalse(notify._email_send("s", "t"))

    def test_seed_refuses_postgres(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgres://x"}):
            with self.assertRaises(SystemExit):
                seed_demo.reset_and_seed()


if __name__ == "__main__":
    unittest.main()
