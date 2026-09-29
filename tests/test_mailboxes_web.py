"""Web-layer Stage-6 tests (plans/v2/06 E): the /mailboxes page, the connect
flow (verification mocked — no sockets), the ramp-clamped cap route, the
go-live checklist gate, per-workspace pause/resume, and workspace scoping of
every mailbox route.

Run (from the repo root):
    python -m unittest discover -s tests -v
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

PASSWORD = "web-app-pass-7788"


class MailboxesWebTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self._key_patch = mock.patch.dict(
            os.environ, {"MAILBOX_ENC_KEY": mailboxes.generate_key()})
        self._key_patch.start()
        self.owner = TestClient(webapp.app)
        signup_owner(self.owner)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.tenant_ws = lead_db.get_user_by_email(TENANT["email"])["workspace_id"]

    def tearDown(self) -> None:
        self._key_patch.stop()
        self.teardown_tenancy()

    def _set_ws(self, workspace_id: int, **fields) -> None:
        con = lead_db.connect()
        try:
            with con.tx():
                for col, val in fields.items():
                    con.execute(
                        f"UPDATE workspaces SET {col} = ? WHERE id = ?",
                        (val, workspace_id))
        finally:
            con.close()

    def _add_box(self, workspace_id: int, address="box@test-co.example",
                 activate=False, days_old=0) -> int:
        mailbox_id = mailboxes.add_mailbox(
            workspace_id, address, PASSWORD,
            smtp_host="smtp.test.example", smtp_port=465,
            imap_host="imap.test.example", imap_port=993)
        if activate:
            con = lead_db.connect()
            try:
                with con.tx():
                    con.execute(
                        "UPDATE mailboxes SET status = 'active', "
                        "warmup_started_at = ? WHERE id = ? AND workspace_id = ?",
                        (db.utcnow_plus(days=-days_old), mailbox_id,
                         workspace_id))
            finally:
                con.close()
        return mailbox_id

    def _msg(self, response) -> str:
        return unquote(response.headers["location"]).replace("+", " ")

    # --- the page ------------------------------------------------------------------

    def test_page_renders_with_empty_state(self):
        r = self.tenant.get("/mailboxes")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No mailbox connected yet", r.text)
        self.assertIn("PRACTICE", r.text)
        self.assertIn("app password", r.text)  # the Gmail walkthrough

    def test_missing_encryption_key_disables_connect(self):
        with mock.patch.dict(os.environ, {"MAILBOX_ENC_KEY": ""}):
            r = self.tenant.get("/mailboxes")
            self.assertIn("encryption key is missing", r.text)
            self.assertNotIn("Connect a mailbox", r.text)
            r = self.tenant.post("/mailboxes/create", data={
                "preset": "gmail", "address": "a@b.example",
                "password": "x"}, follow_redirects=False)
            self.assertIn("Mailbox storage isn't set up", self._msg(r))
        self.assertEqual(mailboxes.list_mailboxes(self.tenant_ws), [])

    # --- connect flow -----------------------------------------------------------------

    def test_create_uses_preset_and_runs_verification(self):
        with mock.patch("mailboxes.verify_mailbox",
                        return_value=(True, "Connected. This mailbox can "
                                            "now send.")) as verify:
            r = self.tenant.post("/mailboxes/create", data={
                "preset": "gmail", "address": "Sales@Test-Co.example",
                "password": PASSWORD}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Connected", self._msg(r))
        boxes = mailboxes.list_mailboxes(self.tenant_ws)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["address"], "sales@test-co.example")
        self.assertEqual(boxes[0]["smtp_host"], "smtp.gmail.com")
        self.assertEqual(boxes[0]["imap_host"], "imap.gmail.com")
        verify.assert_called_once_with(self.tenant_ws, boxes[0]["id"])

    def test_custom_preset_requires_servers(self):
        r = self.tenant.post("/mailboxes/create", data={
            "preset": "custom", "address": "a@b.example",
            "password": PASSWORD}, follow_redirects=False)
        self.assertIn("SMTP and IMAP server names are required", self._msg(r))
        self.assertEqual(mailboxes.list_mailboxes(self.tenant_ws), [])

    def test_mailbox_routes_are_workspace_scoped(self):
        foreign_id = self._add_box(1, "owner@hq.example")  # owner's box
        for path in (f"/mailboxes/{foreign_id}/verify",
                     f"/mailboxes/{foreign_id}/delete"):
            r = self.tenant.post(path, follow_redirects=False)
            self.assertEqual(r.status_code, 404, path)
        r = self.tenant.post(f"/mailboxes/{foreign_id}/cap",
                             data={"daily_cap": "3"}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)
        # Untouched.
        self.assertEqual(mailboxes.list_mailboxes(1)[0]["daily_cap"],
                         mailboxes.DEFAULT_CAP)

    def test_cap_is_clamped_to_the_ramp_with_an_explanation(self):
        mailbox_id = self._add_box(self.tenant_ws, activate=True, days_old=0)
        r = self.tenant.post(f"/mailboxes/{mailbox_id}/cap",
                             data={"daily_cap": "100"}, follow_redirects=False)
        self.assertIn("warmup ramp", self._msg(r))
        self.assertEqual(
            mailboxes.list_mailboxes(self.tenant_ws)[0]["daily_cap"], 5)

    # --- go-live checklist ---------------------------------------------------------------

    def test_golive_checklist_gate(self):
        # 1. Trial plans stay in practice mode, full stop.
        r = self.tenant.post("/mailboxes/golive", data={"warmup_ack": "1"},
                             follow_redirects=False)
        self.assertIn("Trial", self._msg(r))
        self.assertEqual(
            lead_db.get_workspace(self.tenant_ws)["live_sending"], 0)

        # 2. Paid plan, but no postal address yet.
        self._set_ws(self.tenant_ws, plan="starter", status="active")
        r = self.tenant.post("/mailboxes/golive", data={"warmup_ack": "1"},
                             follow_redirects=False)
        self.assertIn("postal address", self._msg(r))

        # 3. Address saved, but no verified mailbox.
        r = self.tenant.post("/mailboxes/identity", data={
            "postal_address": "1 Main St, Boise ID",
            "alert_email": "alerts@test-co.example"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = self.tenant.post("/mailboxes/golive", data={"warmup_ack": "1"},
                             follow_redirects=False)
        self.assertIn("verify at least one mailbox", self._msg(r))

        # 4. Mailbox verified, but the warmup box wasn't ticked.
        self._add_box(self.tenant_ws, activate=True)
        r = self.tenant.post("/mailboxes/golive", data={},
                             follow_redirects=False)
        self.assertIn("warmup", self._msg(r))
        self.assertEqual(
            lead_db.get_workspace(self.tenant_ws)["live_sending"], 0)

        # 5. Checklist complete: live.
        r = self.tenant.post("/mailboxes/golive", data={"warmup_ack": "1"},
                             follow_redirects=False)
        self.assertIn("Live sending is ON", self._msg(r))
        ws = lead_db.get_workspace(self.tenant_ws)
        self.assertEqual(ws["live_sending"], 1)
        self.assertEqual(ws["warmup_ack"], 1)

        # 6. And reversible.
        self.tenant.post("/mailboxes/practice", follow_redirects=False)
        self.assertEqual(
            lead_db.get_workspace(self.tenant_ws)["live_sending"], 0)

    def test_identity_is_saved_and_shown(self):
        self.tenant.post("/mailboxes/identity", data={
            "postal_address": "9 Elm St, Austin TX",
            "alert_email": "Ping@Test-Co.example"}, follow_redirects=False)
        ws = lead_db.get_workspace(self.tenant_ws)
        self.assertEqual(ws["postal_address"], "9 Elm St, Austin TX")
        self.assertEqual(ws["alert_email"], "ping@test-co.example")
        self.assertIn("9 Elm St, Austin TX", self.tenant.get("/mailboxes").text)

    # --- per-workspace pause -----------------------------------------------------------

    def test_workspace_pause_and_resume_routes(self):
        r = self.tenant.post("/campaigns/ws-pause", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(
            lead_db.get_workspace(self.tenant_ws)["outreach_paused"], 1)
        # The other workspace is untouched.
        self.assertEqual(lead_db.get_workspace(1)["outreach_paused"], 0)
        self.tenant.post("/campaigns/ws-resume", follow_redirects=False)
        self.assertEqual(
            lead_db.get_workspace(self.tenant_ws)["outreach_paused"], 0)


if __name__ == "__main__":
    unittest.main()
