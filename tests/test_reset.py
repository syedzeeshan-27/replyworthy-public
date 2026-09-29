"""Workspace data reset (tools/reset_workspace.py) + the Sending page's
outbox query.

The reset is a permanent delete, so the tests that matter are the ones that
prove its blast radius: it empties exactly ONE workspace, it never touches
the global unsubscribe list, and it leaves account-level things (offers,
users, workspaces) standing. list_scheduled_sends is here too because it
reads the same tables and must stay workspace-scoped.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import web.app as webapp
import reset_workspace
from enroll_leads import create_campaign, enroll

try:
    from tests.tenancy import TenancyTestMixin, signup, signup_owner
except ImportError:
    from tenancy import TenancyTestMixin, signup, signup_owner

lead_db = webapp.lead_db

B_USER = "bravo-user@test-co.example"


def seed(workspace_id: int, tag: str, email: str) -> dict:
    """One of everything the reset is supposed to delete."""
    lead_db.upsert_leads(workspace_id, [{
        "name": f"{tag} Salon", "place_id": f"{tag.lower()}-p1",
        "city": "Boise ID", "email": email, "email_verified": "mx_ok",
        "score": 5, "email_subject": f"{tag} subject",
        "email_body": f"{tag} body",
    }])
    lead = lead_db.get_leads(workspace_id)[0]
    offer_id = lead_db.create_offer(workspace_id, {
        "name": f"Offer {tag}",
        "product_description": f"{tag} product.",
        "value_prop": f"{tag} value.",
    })
    campaign_id = create_campaign(workspace_id, f"{tag}Campaign", 30, offer_id)
    enroll(workspace_id, campaign_id, min_score=4, limit=10)
    lead_db.add_note(workspace_id, lead["id"], f"{tag}Note")
    con = lead_db.connect()
    try:
        with con.tx():
            con.execute(
                "INSERT INTO replies (workspace_id, lead_id, mailbox, from_email, "
                "subject, body_text, received_at, imap_uid, classification) "
                "VALUES (?, ?, 'box@x.example', ?, ?, ?, '2026-07-01 10:00:00', ?, "
                "'needs_human')",
                (workspace_id, lead["id"], email, f"{tag} reply subject",
                 f"{tag}Reply", f"INBOX:{tag}-1"),
            )
            con.execute(
                "INSERT INTO alerts (workspace_id, lead_id, kind, severity, title, "
                "body, channel, created_at) VALUES (?, ?, 'needs_human', 'urgent', "
                "?, ?, 'stderr', '2026-07-01 10:00:00')",
                (workspace_id, lead["id"], f"{tag}Alert", f"{tag} alert body"),
            )
    finally:
        con.close()
    return {"lead_id": lead["id"], "offer_id": offer_id,
            "campaign_id": campaign_id}


class TwoWorkspacesMixin(TenancyTestMixin):
    """Workspace 1 (owner) and workspace B, both fully seeded."""

    def setUp(self) -> None:
        self.setup_tenancy()
        self.client_a = TestClient(webapp.app)
        signup_owner(self.client_a)
        self.client_b = TestClient(webapp.app)
        signup(self.client_b, B_USER, "password123", "Test Co")
        self.ws_b = lead_db.get_user_by_email(B_USER)["workspace_id"]
        self.a = seed(1, "Alpha", "alpha@owner-hq.example")
        self.b = seed(self.ws_b, "Bravo", "bravo@test-co.example")

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def counts(self, workspace_id: int) -> dict[str, int]:
        return lead_db.count_workspace_data(workspace_id)


class ResetTestCase(TwoWorkspacesMixin, unittest.TestCase):
    def test_wipes_only_the_target_workspace(self):
        before_a = self.counts(1)
        self.assertTrue(all(n > 0 for n in before_a.values()), before_a)

        deleted = lead_db.reset_workspace_data(self.ws_b)

        after_b = self.counts(self.ws_b)
        self.assertEqual(set(after_b.values()), {0}, after_b)
        # Sequence steps hang off campaigns, not workspace_id — the easiest
        # rows to forget, so assert those landed explicitly.
        self.assertEqual(after_b["sequence_steps"], 0)
        self.assertEqual(self.counts(1), before_a)
        self.assertGreater(deleted["leads"], 0)
        self.assertGreater(deleted["sequence_steps"], 0)

    def test_counts_preview_matches_what_gets_deleted(self):
        preview = self.counts(self.ws_b)
        deleted = lead_db.reset_workspace_data(self.ws_b)
        self.assertEqual(deleted, preview)

    def test_suppression_kept_by_default(self):
        lead_db.add_suppression(self.ws_b, "gone@test-co.example", "bounced")
        lead_db.reset_workspace_data(self.ws_b)
        self.assertTrue(lead_db.is_suppressed(self.ws_b, "gone@test-co.example"))

    def test_include_suppression_flag_clears_it(self):
        lead_db.add_suppression(self.ws_b, "gone@test-co.example", "bounced")
        deleted = lead_db.reset_workspace_data(self.ws_b,
                                               include_suppression=True)
        self.assertEqual(deleted["suppression"], 1)
        self.assertFalse(lead_db.is_suppressed(self.ws_b, "gone@test-co.example"))

    def test_global_unsubscribes_survive_both_modes(self):
        lead_db.add_hard_suppression("optout@anywhere.example", "unsubscribe")
        lead_db.reset_workspace_data(self.ws_b)
        self.assertTrue(lead_db.is_suppressed(self.ws_b, "optout@anywhere.example"))
        lead_db.reset_workspace_data(1, include_suppression=True)
        self.assertTrue(lead_db.is_suppressed(1, "optout@anywhere.example"))

    def test_account_level_rows_survive(self):
        lead_db.reset_workspace_data(self.ws_b)
        self.assertTrue(lead_db.list_offers(self.ws_b))
        self.assertIsNotNone(lead_db.get_workspace(self.ws_b))
        self.assertIsNotNone(lead_db.get_user_by_email(B_USER))


class ResetCliTestCase(TwoWorkspacesMixin, unittest.TestCase):
    def test_preview_run_deletes_nothing(self):
        before = self.counts(self.ws_b)
        rc = reset_workspace.main(["--workspace", str(self.ws_b)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.counts(self.ws_b), before)

    def test_yes_run_deletes(self):
        rc = reset_workspace.main(["--workspace", str(self.ws_b), "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(self.counts(self.ws_b).values()), {0})
        self.assertTrue(lead_db.get_leads(1))  # the other workspace is fine

    def test_unknown_workspace_returns_2(self):
        self.assertEqual(reset_workspace.main(["--workspace", "9999"]), 2)


class ScheduledSendsTestCase(TwoWorkspacesMixin, unittest.TestCase):
    def test_is_workspace_scoped(self):
        rows = lead_db.list_scheduled_sends(self.ws_b)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["lead_id"], self.b["lead_id"])
        for row in rows:
            self.assertEqual(row["lead_email"], "bravo@test-co.example")
            self.assertEqual(row["campaign_name"], "BravoCampaign")
            self.assertEqual(row["lead_name"], "Bravo Salon")

    def test_soonest_first_and_limit_honored(self):
        # A second enrolled lead, so there are two pending sends to order.
        lead_db.upsert_leads(self.ws_b, [{
            "name": "Bravo Two", "place_id": "bravo-p2", "city": "Boise ID",
            "email": "bravo2@test-co.example", "email_verified": "mx_ok",
            "score": 5, "email_subject": "s", "email_body": "b",
        }])
        enroll(self.ws_b, self.b["campaign_id"], min_score=4, limit=10)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE sends SET scheduled_at = '2030-03-03 09:00:00' "
                    "WHERE workspace_id = ?", (self.ws_b,))
                con.execute(
                    "UPDATE sends SET scheduled_at = '2029-01-01 08:00:00' "
                    "WHERE workspace_id = ? AND lead_id = "
                    "(SELECT MAX(lead_id) FROM sends WHERE workspace_id = ?)",
                    (self.ws_b, self.ws_b))
        finally:
            con.close()
        rows = lead_db.list_scheduled_sends(self.ws_b)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["scheduled_at"], "2029-01-01 08:00:00")
        self.assertEqual(rows[0]["lead_name"], "Bravo Two")
        self.assertEqual(len(lead_db.list_scheduled_sends(self.ws_b, limit=1)), 1)

    def test_only_scheduled_status_counts(self):
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE sends SET status = 'sent' WHERE workspace_id = ?",
                    (self.ws_b,))
        finally:
            con.close()
        self.assertEqual(lead_db.list_scheduled_sends(self.ws_b), [])


if __name__ == "__main__":
    unittest.main()
