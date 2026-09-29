"""Web layer for the in-app reply feature: the reply box renders where a human
would use it, and POST /lead/{id}/reply relays the outcome (blank body, practice
mode, cross-workspace) back as a redirect message. The send mechanics themselves
are covered in tests/test_manual_reply.py; this pins the wiring.

Run: python -m unittest tests.test_web_reply -v
"""
from __future__ import annotations

import os
import unittest
from unittest import mock
from urllib.parse import unquote

from fastapi.testclient import TestClient

import web.app as webapp
import db
import mailboxes

try:
    from tests.tenancy import TenancyTestMixin, TENANT, signup_owner, signup_tenant
except ImportError:
    from tenancy import TenancyTestMixin, TENANT, signup_owner, signup_tenant

lead_db = webapp.lead_db


class ReplyWebTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self._key = mock.patch.dict(
            os.environ, {"MAILBOX_ENC_KEY": mailboxes.generate_key()})
        self._key.start()
        self.owner = TestClient(webapp.app)
        signup_owner(self.owner)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.ws = lead_db.get_user_by_email(TENANT["email"])["workspace_id"]
        self.lead_id = self._seed_interested_lead(self.ws)

    def tearDown(self) -> None:
        self._key.stop()
        self.teardown_tenancy()

    def _seed_interested_lead(self, ws_id, email="prospect@acme.example"):
        con = lead_db.connect()
        try:
            with con.tx():
                lead_id = con.insert_id(
                    "INSERT INTO leads (workspace_id, place_id, name, city, "
                    "email, email_verified, score, email_subject, status, "
                    "first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ws_id, f"pl-{ws_id}", "Acme Co", "Boise ID", email, "mx_ok",
                     5, "quick question", "interested", db.utcnow(), db.utcnow()))
        finally:
            con.close()
        return lead_id

    def _activate_box(self, ws_id, address="box@tenant.example"):
        domain = address.split("@")[1]
        mbx = mailboxes.add_mailbox(
            ws_id, address, "app-pass-web-1234",
            smtp_host=f"smtp.{domain}", smtp_port=465,
            imap_host=f"imap.{domain}", imap_port=993)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute("UPDATE mailboxes SET status='active', "
                            "warmup_started_at=? WHERE id=?",
                            (db.utcnow_plus(days=-30), mbx))
        finally:
            con.close()
        return mbx

    # --- the box shows up where the human works -----------------------------

    def test_reply_form_renders_on_queue(self):
        page = self.tenant.get("/queue")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"/lead/{self.lead_id}/reply", page.text)
        self.assertIn("Send reply", page.text)

    def test_reply_form_renders_on_lead_detail(self):
        page = self.tenant.get(f"/lead/{self.lead_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"/lead/{self.lead_id}/reply", page.text)

    # --- the route relays outcomes ------------------------------------------

    def test_blank_reply_is_rejected(self):
        r = self.tenant.post(f"/lead/{self.lead_id}/reply",
                             data={"body": "   ", "back": "/queue"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Type a reply", unquote(r.headers["location"]))

    def test_practice_mode_message(self):
        self._activate_box(self.ws)  # trial workspace stays practice-only
        r = self.tenant.post(f"/lead/{self.lead_id}/reply",
                             data={"body": "Happy to help — here are some times.",
                                   "back": "/queue"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Practice mode", unquote(r.headers["location"]))

    def test_protocol_relative_back_is_rejected(self):
        # `back=//evil.example` must not become an off-site redirect target.
        r = self.tenant.post(f"/lead/{self.lead_id}/reply",
                             data={"body": "   ", "back": "//evil.example/x"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        loc = r.headers["location"]
        self.assertFalse(loc.startswith("//"), loc)
        self.assertTrue(loc.startswith("/queue"), loc)

    def test_cannot_reply_to_another_workspaces_lead(self):
        # The owner (workspace 1) cannot reply to the tenant's lead.
        r = self.owner.post(f"/lead/{self.lead_id}/reply",
                            data={"body": "sneaky", "back": "/queue"},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("not found", unquote(r.headers["location"]).lower())


if __name__ == "__main__":
    unittest.main()
