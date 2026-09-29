"""Web-layer billing tests (plans/v2/05 E): the status machine enforced on
every spend/send route, per-plan search caps applied server-side, the public
webhook path, the Billing page's honest no-provider state, and the admin
freeze/adjust levers. The sequencer's billing gate is covered here too.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import unquote

from fastapi.testclient import TestClient

import web.app as webapp
import billing
import sequencer
from enroll_leads import create_campaign, enroll

try:
    from tests.tenancy import TenancyTestMixin, TENANT, signup_owner, signup_tenant
except ImportError:
    from tenancy import TenancyTestMixin, TENANT, signup_owner, signup_tenant

lead_db = webapp.lead_db

# Wednesday 18:00 UTC = mid-morning across US zones (inside the send window).
IN_WINDOW = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)

LEAD = {
    "name": "Window Dental", "place_id": "bw-1", "city": "Boise ID",
    "email": "billing@prospect.example", "email_verified": "mx_ok",
    "score": 5, "email_subject": "hi", "email_body": "hello body",
}


class BillingWebTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.owner = TestClient(webapp.app)
        signup_owner(self.owner)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.tenant_ws = lead_db.get_user_by_email(TENANT["email"])["workspace_id"]

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def _set_ws(self, workspace_id: int, **fields) -> None:
        con = lead_db.connect()
        try:
            with con.tx():
                for col, val in fields.items():
                    con.execute(
                        f"UPDATE workspaces SET {col} = ? WHERE id = ?",
                        (val, workspace_id),
                    )
        finally:
            con.close()

    # --- the Billing page -----------------------------------------------------

    def test_billing_page_shows_credits_and_honest_provider_state(self):
        r = self.tenant.get("/billing")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Credits left", r.text)
        self.assertIn("Trial", r.text)
        # No Dodo account yet: the page says so, no upgrade buttons pretend.
        self.assertIn("aren't connected yet", r.text)
        self.assertNotIn("checkout.dodopayments.com", r.text)

    def test_admin_link_only_for_the_admin(self):
        self.assertIn("/admin/billing", self.owner.get("/billing").text)
        self.assertNotIn("/admin/billing", self.tenant.get("/billing").text)

    # --- webhook route ----------------------------------------------------------

    def test_webhook_is_public_but_signature_gated(self):
        fresh = TestClient(webapp.app)  # no session at all
        r = fresh.post("/billing/webhook", content=b"{}")
        # 400 (bad signature), NOT a 303 redirect to /login: public path,
        # HMAC is the gate.
        self.assertEqual(r.status_code, 400)

    # --- spend/send gating per status --------------------------------------------

    def test_jobs_blocked_for_past_due_but_reading_still_works(self):
        self._set_ws(self.tenant_ws, status="past_due")
        with mock.patch.object(webapp, "start_job") as started:
            r = self.tenant.post("/jobs", data={
                "category": "dentist", "city": "Boise ID", "limit": "5"})
        self.assertEqual(r.status_code, 402)
        self.assertIn("payment issue", r.text)
        started.assert_not_called()
        # Read access stays: data intact, pages load.
        self.assertEqual(self.tenant.get("/").status_code, 200)
        self.assertEqual(self.tenant.get("/pipeline").status_code, 200)

    def test_jobs_blocked_without_credits(self):
        billing.debit_leads(self.tenant_ws, 20, 0, ref="drain")  # 5 left
        with mock.patch.object(webapp, "start_job") as started:
            r = self.tenant.post("/jobs", data={
                "category": "dentist", "city": "Boise ID", "limit": "10"})
        self.assertEqual(r.status_code, 402)
        self.assertIn("Not enough credits", r.text)
        started.assert_not_called()

    def test_search_count_clamped_to_plan_cap(self):
        # Trial cap is 25: asking for 200 must be trimmed server-side.
        with mock.patch.object(webapp, "start_job",
                               return_value="fake123") as started:
            r = self.tenant.post("/jobs", data={
                "category": "dentist", "city": "Boise ID", "limit": "200"},
                follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(started.call_args.args[3], 25)  # the limit argument

    def test_frozen_workspace_cannot_start_a_search(self):
        billing.set_frozen(self.tenant_ws, True)
        with mock.patch.object(webapp, "start_job") as started:
            r = self.tenant.post("/jobs", data={
                "category": "dentist", "city": "Boise ID", "limit": "10"})
        self.assertEqual(r.status_code, 402)
        self.assertIn("frozen", r.text)
        started.assert_not_called()  # nothing paid for

    def test_enroll_blocked_when_canceled(self):
        lead_db.upsert_leads(self.tenant_ws, [dict(LEAD)])
        campaign_id = create_campaign(self.tenant_ws, "C1", 30)
        self._set_ws(self.tenant_ws, status="canceled")
        r = self.tenant.post(f"/campaigns/{campaign_id}/enroll",
                             data={"min_score": "4", "limit": "10"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("ended", unquote(r.headers["location"]))
        con = lead_db.connect()
        try:
            n = con.execute(
                "SELECT COUNT(*) AS n FROM sends WHERE workspace_id = ?",
                (self.tenant_ws,),
            ).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            con.close()

    # --- admin levers --------------------------------------------------------------

    def test_admin_billing_page_is_admin_only(self):
        self.assertEqual(self.tenant.get("/admin/billing").status_code, 403)
        r = self.owner.get("/admin/billing")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test Co", r.text)

    def test_freeze_blocks_instantly_and_unfreeze_restores(self):
        r = self.owner.post(f"/admin/billing/{self.tenant_ws}/freeze",
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with mock.patch.object(webapp, "start_job") as started:
            blocked = self.tenant.post("/jobs", data={
                "category": "dentist", "city": "Boise ID", "limit": "5"})
        self.assertEqual(blocked.status_code, 402)
        started.assert_not_called()

        self.owner.post(f"/admin/billing/{self.tenant_ws}/freeze",
                        data={"unfreeze": "1"}, follow_redirects=False)
        with mock.patch.object(webapp, "start_job", return_value="fake123"):
            ok = self.tenant.post("/jobs", data={
                "category": "dentist", "city": "Boise ID", "limit": "5"},
                follow_redirects=False)
        self.assertEqual(ok.status_code, 303)

    def test_freeze_route_is_admin_only(self):
        r = self.tenant.post(f"/admin/billing/{self.tenant_ws}/freeze")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(lead_db.get_workspace(self.tenant_ws)["frozen"])

    def test_admin_adjust_writes_a_ledger_row(self):
        r = self.owner.post(f"/admin/billing/{self.tenant_ws}/adjust",
                            data={"delta": "100", "note": "goodwill"},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(billing.balance(self.tenant_ws), 125)
        rows = billing.ledger_rows(self.tenant_ws)
        self.assertEqual(rows[0]["reason"], "adjustment")

    # --- the sequencer's billing gate -------------------------------------------------

    def test_sequencer_skips_nonpaying_and_resumes_on_recovery(self):
        self._set_ws(self.tenant_ws, plan="starter", status="active")
        lead_db.upsert_leads(self.tenant_ws, [dict(LEAD)])
        campaign_id = create_campaign(self.tenant_ws, "C1", 30)
        self.assertEqual(
            enroll(self.tenant_ws, campaign_id, min_score=4, limit=10)["enrolled"], 1)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute("UPDATE sends SET scheduled_at = ? WHERE workspace_id = ?",
                            ("2026-07-01 00:00:00", self.tenant_ws))
        finally:
            con.close()

        self._set_ws(self.tenant_ws, status="past_due")
        counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)
        self.assertEqual(counters["sent"], 0)
        self.assertGreaterEqual(counters["skipped_billing"], 1)

        # Recovery: the same scheduled send goes out untouched.
        self._set_ws(self.tenant_ws, status="active")
        counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)
        self.assertEqual(counters["sent"], 1)


class PlanCapabilityGateTestCase(TenancyTestMixin, unittest.TestCase):
    """The three server-side tier gates added 2026-07-23: offer profiles,
    active campaigns, and person-lead search. Each is enforced at the route,
    so the UI can't be talked around with a hand-built POST."""

    _OFFER = {
        "name": "Second offer",
        "product_description": "A booking widget for salon websites that "
                               "lets clients self-book and cuts no-shows.",
        "value_prop": "Fewer no-shows, more booked chairs.",
    }
    # lead_type person routes to b2b_data_api (parse_query.route_source).
    _PERSON_FORM = {"lead_type": "person", "category": "marketing agencies",
                    "city": "Austin TX", "limit": "10"}

    def setUp(self) -> None:
        self.setup_tenancy()
        self.owner = TestClient(webapp.app)
        signup_owner(self.owner)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.tenant_ws = lead_db.get_user_by_email(TENANT["email"])["workspace_id"]

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def _set_plan(self, plan: str) -> None:
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE workspaces SET plan = ?, status = 'active' "
                    "WHERE id = ?", (plan, self.tenant_ws))
        finally:
            con.close()

    # --- offer profiles ------------------------------------------------------

    def test_offer_limit_blocks_trial_and_starter_not_growth(self):
        # A new workspace already holds its seeded default offer, so on
        # trial/starter (max_offers 1) the very next create must bounce.
        r = self.tenant.post("/offers/create", data=self._OFFER)
        self.assertEqual(r.status_code, 403)
        self.assertIn("1 offer profile", r.text)
        self.assertIn("Upgrade", r.text)
        self.assertEqual(len(lead_db.list_offers(self.tenant_ws)), 1)

        self._set_plan("growth")  # max_offers 5
        r = self.tenant.post("/offers/create", data=self._OFFER,
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(lead_db.list_offers(self.tenant_ws)), 2)

    # --- active campaigns ----------------------------------------------------

    def test_campaign_limit_counts_only_active_campaigns(self):
        self._set_plan("trial")  # max_campaigns 1
        r = self.tenant.post("/campaigns/create",
                             data={"name": "C1", "daily_cap": 30},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("Your+plan+includes", r.headers["location"])

        r = self.tenant.post("/campaigns/create",
                             data={"name": "C2", "daily_cap": 30},
                             follow_redirects=False)
        self.assertIn("1%20active%20campaign", r.headers["location"])

        # Pausing the first frees the slot — the gate counts ACTIVE only.
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE campaigns SET status = 'paused' "
                    "WHERE workspace_id = ?", (self.tenant_ws,))
        finally:
            con.close()
        r = self.tenant.post("/campaigns/create",
                             data={"name": "C2", "daily_cap": 30},
                             follow_redirects=False)
        self.assertNotIn("Your%20plan%20includes", r.headers["location"])

    # --- person-lead search --------------------------------------------------

    def test_person_search_blocked_below_growth(self):
        r = self.tenant.post("/jobs", data=self._PERSON_FORM)
        self.assertEqual(r.status_code, 403)
        self.assertIn("Growth and Scale", r.text)

        # On growth the plan gate passes; the next honest gate (no B2B
        # account configured in tests) answers instead — proving the plan
        # check fires FIRST and with a different message.
        self._set_plan("growth")
        with mock.patch.dict("os.environ",
                             {"B2B_DATA_API_KEY": "", "B2B_DATA_API_URL": ""}):
            r = self.tenant.post("/jobs", data=self._PERSON_FORM)
        self.assertEqual(r.status_code, 400)
        self.assertIn("contact-database account", r.text)
        self.assertNotIn("Growth and Scale", r.text)


if __name__ == "__main__":
    unittest.main()
