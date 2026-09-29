"""Two-tier suppression tests (plans/v2/04 B + E4): per-workspace lists plus
the global hard list, enforced in BOTH the enrollment path and the send path,
plus the workspace-encoded unsubscribe tokens (plans/v2/04 D).

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp
import process_replies
import send_email
import sequencer
from enroll_leads import create_campaign, enroll

try:
    from tests.tenancy import TenancyTestMixin, signup, signup_owner
except ImportError:
    from tenancy import TenancyTestMixin, signup, signup_owner

lead_db = webapp.lead_db

# Wednesday 18:00 UTC = mid-morning across US zones — inside the send window.
IN_WINDOW = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)

LEAD = {
    "name": "Window Dental", "place_id": "wd-1", "city": "Boise ID",
    "email": "shared@prospect.example", "email_verified": "mx_ok",
    "score": 5, "email_subject": "hi", "email_body": "hello body",
}


def _seed_enrollable(workspace_id: int, place_id: str) -> int:
    lead_db.upsert_leads(workspace_id, [{**LEAD, "place_id": place_id}])
    return lead_db.get_leads(workspace_id)[0]["id"]


class SuppressionTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.ws2 = lead_db.create_workspace("Test Co")

    def tearDown(self) -> None:
        self.teardown_tenancy()

    # --- tiers -----------------------------------------------------------

    def test_workspace_suppression_does_not_cross_workspaces(self):
        lead_db.add_suppression(1, "shared@prospect.example", "not_interested")
        self.assertTrue(lead_db.is_suppressed(1, "shared@prospect.example"))
        self.assertFalse(lead_db.is_suppressed(self.ws2, "shared@prospect.example"))

    def test_hard_suppression_blocks_every_workspace(self):
        lead_db.add_hard_suppression("shared@prospect.example", "unsubscribed")
        self.assertTrue(lead_db.is_suppressed(1, "shared@prospect.example"))
        self.assertTrue(lead_db.is_suppressed(self.ws2, "shared@prospect.example"))

    # --- enrollment path ---------------------------------------------------

    def test_enrollment_respects_both_tiers(self):
        _seed_enrollable(1, "e1")
        _seed_enrollable(self.ws2, "e2")
        c1 = create_campaign(1, "C1", 30)
        c2 = create_campaign(self.ws2, "C2", 30)

        # Workspace 1 suppressed the address; workspace 2 can still enroll it.
        lead_db.add_suppression(1, LEAD["email"], "not_interested")
        self.assertEqual(enroll(1, c1, min_score=4, limit=10)["skipped_suppressed"], 1)
        self.assertEqual(enroll(self.ws2, c2, min_score=4, limit=10)["enrolled"], 1)

        # Hard-unsubscribe it: now NOBODY can enroll it.
        lead_db.add_hard_suppression(LEAD["email"], "unsubscribed")
        _seed_enrollable(self.ws2, "e3")  # a fresh 'new' lead, same address
        counters = enroll(self.ws2, c2, min_score=4, limit=10)
        self.assertEqual(counters["enrolled"], 0)
        self.assertEqual(counters["skipped_suppressed"], 1)

    # --- send path ---------------------------------------------------------

    def test_send_path_cancels_when_hard_suppressed_after_enrollment(self):
        _seed_enrollable(1, "s1")
        c1 = create_campaign(1, "C1", 30)
        self.assertEqual(enroll(1, c1, min_score=4, limit=10)["enrolled"], 1)
        # The prospect hard-unsubscribes between enrollment and the tick.
        lead_db.add_hard_suppression(LEAD["email"], "unsubscribed")
        # Force the scheduled send due now.
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute("UPDATE sends SET scheduled_at = ? "
                            "WHERE workspace_id = 1", ("2026-07-01 00:00:00",))
        finally:
            con.close()
        counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)
        self.assertEqual(counters["cancelled"], 1)
        self.assertEqual(counters["sent"], 0)

    def test_send_path_sends_when_only_another_workspace_suppressed(self):
        _seed_enrollable(1, "s2")
        c1 = create_campaign(1, "C1", 30)
        enroll(1, c1, min_score=4, limit=10)
        lead_db.add_suppression(self.ws2, LEAD["email"], "not_interested")
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute("UPDATE sends SET scheduled_at = ? "
                            "WHERE workspace_id = 1", ("2026-07-01 00:00:00",))
        finally:
            con.close()
        counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)
        self.assertEqual(counters["sent"], 1)  # DRY_RUN send, nothing real

    # --- reply-driven unsubscribe goes hard ---------------------------------

    def test_unsubscribe_reply_hard_suppresses_globally(self):
        lead_id = _seed_enrollable(1, "r1")
        lead_db.set_status(1, lead_id, "contacted")
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "INSERT INTO replies (workspace_id, lead_id, mailbox, "
                    "from_email, subject, body_text, received_at, imap_uid) "
                    "VALUES (1, ?, 'box@x.example', ?, 'stop', "
                    "'take me off this list', '2026-07-01 10:00:00', 'INBOX:9')",
                    (lead_id, LEAD["email"]),
                )
        finally:
            con.close()
        classifier = mock.Mock(return_value={
            "classification": "unsubscribe", "confidence": "high",
            "summary": "asked to stop", "suggested_action": "",
        })
        process_replies.process_pending(classifier=classifier)
        self.assertTrue(lead_db.is_suppressed(self.ws2, LEAD["email"]),
                        "an explicit unsubscribe must block every workspace")
        self.assertEqual(lead_db.get_lead(1, lead_id)["status"], "unsubscribed")

    # --- unsubscribe tokens encode the workspace -----------------------------

    def test_unsub_token_roundtrip_and_workspace_binding(self):
        token = send_email.make_unsub_token(1, 42)
        self.assertEqual(send_email.verify_unsub_token(token), (1, 42))
        self.assertIsNone(send_email.verify_unsub_token(token[:-3] + "xyz"))

    def test_unsubscribe_endpoint_scopes_to_the_tokens_workspace(self):
        client = TestClient(webapp.app)
        signup_owner(client)
        c2 = TestClient(webapp.app)
        signup(c2, "b@test-co.example", "password123", "Test Co")
        ws2 = lead_db.get_user_by_email("b@test-co.example")["workspace_id"]

        a_lead = _seed_enrollable(1, "u1")
        b_lead = _seed_enrollable(ws2, "u2")  # same email, other workspace

        token = send_email.make_unsub_token(1, a_lead)
        # Public route — works logged out.
        r = TestClient(webapp.app).get(f"/unsubscribe/{token}")
        self.assertEqual(r.status_code, 200)

        # A's lead flipped; B's lead status untouched…
        self.assertEqual(lead_db.get_lead(1, a_lead)["status"], "unsubscribed")
        self.assertEqual(lead_db.get_lead(ws2, b_lead)["status"], "new")
        # …but the address is hard-blocked everywhere (global on purpose).
        self.assertTrue(lead_db.is_suppressed(ws2, LEAD["email"]))


if __name__ == "__main__":
    unittest.main()
