"""The leakage suite (plans/v2/04 D+E3) — the business-ending-bug hunt.

Two workspaces are fully seeded (leads, offers, campaigns, sends, replies,
notes, alerts). Logged in as workspace A, EVERY page must contain zero
workspace-B bytes, and every POST against a B object id must 404. If any of
these fail, one missed filter would email workspace B's leads from workspace
A's identity — that's the bug this file exists to catch.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import web.app as webapp
from enroll_leads import create_campaign, enroll

try:
    from tests.tenancy import TenancyTestMixin, signup, signup_owner
except ImportError:
    from tenancy import TenancyTestMixin, signup, signup_owner

lead_db = webapp.lead_db

# Every B-marker below must NEVER appear in a page rendered for workspace A.
B_MARKERS = (
    "Bravo Salon", "bravo@test-co.example", "Offer Bravo",
    "BravoCampaign", "BravoNote", "BravoAlert", "BravoReply",
)


def seed_workspace(workspace_id: int, tag: str, email: str) -> dict:
    """Fill one workspace with one of everything. Returns the created ids."""
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
        # Queue visibility: the lead waits for a human.
        lead_db.set_status(workspace_id, lead["id"], "needs_human")
    finally:
        con.close()
    return {"lead_id": lead["id"], "offer_id": offer_id,
            "campaign_id": campaign_id}


class TenancyLeakageTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        # Workspace A = the owner (admin, workspace 1).
        self.client_a = TestClient(webapp.app)
        signup_owner(self.client_a)
        # Workspace B = a plain member with their own workspace.
        self.client_b = TestClient(webapp.app)
        signup(self.client_b, "bravo-user@test-co.example", "password123",
               "Test Co")
        self.ws_b = lead_db.get_user_by_email(
            "bravo-user@test-co.example")["workspace_id"]

        self.a = seed_workspace(1, "Alpha", "alpha@owner-hq.example")
        self.b = seed_workspace(self.ws_b, "Bravo", "bravo@test-co.example")

    def tearDown(self) -> None:
        self.teardown_tenancy()

    # --- every GET as A: zero B bytes ---------------------------------------

    def test_every_page_as_a_shows_zero_b_bytes(self):
        # The live-search fragment (?partial=1) is its own response path —
        # it has to be as workspace-scoped as the full page.
        pages = ("/", "/pipeline", "/pipeline?status=suppressed",
                 "/pipeline?q=Salon", "/pipeline?q=Salon&partial=1", "/queue",
                 "/campaigns", "/offers", f"/lead/{self.a['lead_id']}",
                 f"/offers?edit={self.a['offer_id']}")
        for page in pages:
            r = self.client_a.get(page)
            self.assertEqual(r.status_code, 200, page)
            # Sanity: the page really rendered A's world…
            if page not in ("/pipeline?status=suppressed",):
                self.assertIn("Alpha", r.text, page)
            # …and nothing of B's leaked into it.
            for marker in B_MARKERS:
                self.assertNotIn(marker, r.text,
                                 f"workspace-B marker {marker!r} leaked into {page}")

    def test_b_sees_none_of_a(self):
        for page in ("/", "/pipeline", "/pipeline?q=Salon&partial=1", "/queue",
                     "/campaigns", "/offers"):
            r = self.client_b.get(page)
            self.assertEqual(r.status_code, 200, page)
            self.assertNotIn("Alpha", r.text, page)
            self.assertNotIn("alpha@owner-hq.example", r.text, page)

    # --- direct object access with B's ids: 404, never data ------------------

    def test_get_bs_lead_as_a_is_404(self):
        r = self.client_a.get(f"/lead/{self.b['lead_id']}")
        self.assertEqual(r.status_code, 404)
        for marker in B_MARKERS:
            self.assertNotIn(marker, r.text)

    def test_every_post_as_a_against_bs_ids_is_404(self):
        posts = (
            (f"/lead/{self.b['lead_id']}/status", {"status": "won"}),
            (f"/lead/{self.b['lead_id']}/notes", {"body": "intrusion"}),
            (f"/offers/{self.b['offer_id']}/update",
             {"name": "hijack", "product_description": "x", "value_prop": "y"}),
            (f"/offers/{self.b['offer_id']}/delete", {}),
            (f"/offers/{self.b['offer_id']}/default", {}),
            (f"/campaigns/{self.b['campaign_id']}/toggle", {}),
            (f"/campaigns/{self.b['campaign_id']}/enroll",
             {"min_score": "1", "limit": "10"}),
        )
        for path, data in posts:
            r = self.client_a.post(path, data=data, follow_redirects=False)
            self.assertEqual(r.status_code, 404, path)
        # Nothing changed on B's side.
        b_lead = lead_db.get_lead(self.ws_b, self.b["lead_id"])
        self.assertEqual(b_lead["status"], "needs_human")
        self.assertEqual(len(lead_db.get_notes(self.ws_b, self.b["lead_id"])), 1)
        b_offer = lead_db.get_offer(self.ws_b, self.b["offer_id"])
        self.assertEqual(b_offer["name"], "Offer Bravo")
        self.assertEqual(b_offer["deleted"], 0)

    # --- member B cannot toggle A's campaign either ---------------------------

    def test_every_post_as_b_against_as_ids_is_404(self):
        posts = (
            (f"/lead/{self.a['lead_id']}/status", {"status": "lost"}),
            (f"/offers/{self.a['offer_id']}/delete", {}),
            (f"/campaigns/{self.a['campaign_id']}/toggle", {}),
        )
        for path, data in posts:
            r = self.client_b.post(path, data=data, follow_redirects=False)
            self.assertEqual(r.status_code, 404, path)

    # --- workers keep the wall too --------------------------------------------

    def test_sends_were_created_workspace_scoped(self):
        con = lead_db.connect()
        try:
            rows = con.execute(
                "SELECT workspace_id, lead_id FROM sends ORDER BY id"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 2)  # one enrollment per workspace
        by_ws = {r["workspace_id"]: r["lead_id"] for r in rows}
        self.assertEqual(by_ws[1], self.a["lead_id"])
        self.assertEqual(by_ws[self.ws_b], self.b["lead_id"])

    def test_dashboard_counts_are_per_workspace(self):
        r_a = self.client_a.get("/")
        r_b = self.client_b.get("/")
        # Each dashboard shows exactly its one lead, not two.
        self.assertIn("Alpha", r_a.text)
        self.assertNotIn("Bravo", r_a.text)
        self.assertIn("Bravo", r_b.text)
        self.assertNotIn("Alpha", r_b.text)


if __name__ == "__main__":
    unittest.main()
