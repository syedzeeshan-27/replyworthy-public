"""Billing + metering unit tests (plans/v2/05 E): the credit ledger, the
pre-run gate, the rollover-capped monthly grant, and the Dodo webhook
handler — signature verification, idempotent replays, all four normalized
events. Zero live provider calls: every event is a locally signed fixture.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest import mock

try:
    from tests.tenancy import TenancyTestMixin  # also puts tools/ on sys.path
except ImportError:
    from tenancy import TenancyTestMixin

import billing
import lead_db

# A locally generated webhook secret in the provider's whsec_ format —
# a test value, not a real credential.
TEST_KEY = b"unit-test-webhook-key-32-bytes!!"
TEST_SECRET = "whsec_" + base64.b64encode(TEST_KEY).decode()


def signed_headers(body: bytes, msg_id: str = "evt_1",
                   timestamp: int | None = None) -> dict:
    """Sign a payload exactly the way the provider would (Standard Webhooks)."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    mac = hmac.new(TEST_KEY, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": "v1," + base64.b64encode(mac.digest()).decode(),
    }


def event_body(event_type: str, workspace_id: int | None,
               plan: str = "starter", sub_id: str = "sub_123",
               with_metadata: bool = True) -> bytes:
    data = {"subscription_id": sub_id, "product_id": "prod_x",
            "customer": {"customer_id": "cus_9"}}
    if with_metadata:
        data["metadata"] = {"workspace_id": str(workspace_id), "plan": plan,
                            "sig": billing.metadata_sig(workspace_id, plan)}
    return json.dumps({"id": "payload_id", "type": event_type,
                       "data": data}).encode()


class PricingLadderTestCase(unittest.TestCase):
    """Guards the pricing shape decided 2026-07-22. Pure constants, no DB.

    These are not style checks. The ladder really did invert once (scale cost
    $0.253/credit against growth's $0.239), which made upgrading to the top
    plan a worse deal per lead and quietly went margin-negative on
    person-heavy workspaces. If one of these fails after someone edits a
    number, the pricing is broken, not the test.
    """

    LADDER = ("starter", "growth", "scale")

    def _per_credit(self, key: str) -> float:
        p = billing.PLANS[key]
        return p["price_usd"] / p["monthly_credits"]

    def test_cost_per_credit_strictly_decreases_up_the_ladder(self):
        rates = [self._per_credit(k) for k in self.LADDER]
        for lower, higher, cheap, dear in zip(self.LADDER, self.LADDER[1:],
                                              rates, rates[1:]):
            self.assertLess(
                dear, cheap,
                f"{higher} costs ${dear:.3f}/credit but {lower} only "
                f"${cheap:.3f} — upgrading must always lower the cost per "
                f"lead, or nobody has a reason to move up.",
            )

    def test_credits_and_price_both_climb_with_the_tier(self):
        for lower, higher in zip(self.LADDER, self.LADDER[1:]):
            self.assertLess(billing.PLANS[lower]["price_usd"],
                            billing.PLANS[higher]["price_usd"])
            self.assertLess(billing.PLANS[lower]["monthly_credits"],
                            billing.PLANS[higher]["monthly_credits"])

    def test_abuse_rails_identical_across_paid_plans(self):
        """search_cap and daily_send_cap are anti-abuse guards, not tier
        features (2026-07-23 shape). They stay identical across paid plans
        and are never advertised; the sellable differences are credits plus
        the four capability limits checked below."""
        for knob in ("search_cap", "daily_send_cap"):
            values = {billing.PLANS[k][knob] for k in self.LADDER}
            self.assertEqual(
                len(values), 1,
                f"{knob} differs across paid plans ({values}). It is an abuse "
                f"rail, not a tier feature — hold it identical.",
            )

    def test_capability_limits_never_decrease_up_the_ladder(self):
        """The four enforced tier levers (person_leads, max_offers,
        max_campaigns, rollover_multiple) must be monotonic: a higher tier
        never takes away what a lower tier had. None = unlimited."""
        as_num = {
            "person_leads": lambda v: 1 if v else 0,
            "max_offers": lambda v: float("inf") if v is None else v,
            "max_campaigns": lambda v: float("inf") if v is None else v,
            "rollover_multiple": lambda v: v,
        }
        for knob, num in as_num.items():
            values = [num(billing.PLANS[k][knob]) for k in self.LADDER]
            for lower, higher, lo, hi in zip(self.LADDER, self.LADDER[1:],
                                             values, values[1:]):
                self.assertGreaterEqual(
                    hi, lo,
                    f"{knob}: {higher} ({hi}) is below {lower} ({lo}) — an "
                    f"upgrade must never remove a capability.",
                )

    def test_person_leads_start_at_growth(self):
        """Person leads hit the paid B2B vendor (2-credit, thin-margin path)
        and are deliberately gated off starter and trial."""
        self.assertFalse(billing.PLANS["trial"]["person_leads"])
        self.assertFalse(billing.PLANS["starter"]["person_leads"])
        self.assertTrue(billing.PLANS["growth"]["person_leads"])
        self.assertTrue(billing.PLANS["scale"]["person_leads"])

    def test_no_paid_plan_promises_unlimited_sending(self):
        """daily_send_cap None on a paid plan reads as 'unlimited daily
        sending', which no mail provider will honor: Google hard-caps
        2k/day/mailbox and safe cold volume is 20-50. Only the owner's
        unmetered workspace may be uncapped."""
        for key in self.LADDER:
            self.assertIsNotNone(
                billing.PLANS[key]["daily_send_cap"],
                f"{key} has no send ceiling — that is a promise we cannot keep.",
            )

    def test_person_leads_still_cost_more_than_business(self):
        """If this ratio ever changes, the disclosure on landing/index.html,
        landing/terms.html and web/templates/billing.html must change with
        it — a customer who expects 200 leads from 200 credits and gets 100
        has a fair refund argument."""
        self.assertGreater(billing.CREDIT_COST["person"],
                           billing.CREDIT_COST["business"])


class BillingLedgerTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.ws2 = lead_db.create_workspace("Test Co")

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

    # --- grants and debits -------------------------------------------------

    def test_new_workspace_gets_trial_grant(self):
        ws = lead_db.get_workspace(self.ws2)
        self.assertEqual(ws["plan"], "trial")
        self.assertEqual(ws["credits_balance"], 25)
        self.assertTrue(ws["trial_expires_at"])
        rows = billing.ledger_rows(self.ws2)
        self.assertEqual([(r["reason"], r["delta"]) for r in rows],
                         [("trial_grant", 25)])

    def test_owner_workspace_is_internal_and_unmetered(self):
        self.assertEqual(lead_db.get_workspace(1)["plan"], "internal")
        self.assertEqual(billing.debit_leads(1, 5, 2, ref="x"), 0)
        self.assertEqual(billing.ledger_rows(1), [])

    def test_debit_math_business_and_person(self):
        charged = billing.debit_leads(self.ws2, 3, 2, ref="run-1")
        self.assertEqual(charged, 3 * 1 + 2 * 2)
        self.assertEqual(billing.balance(self.ws2), 25 - 7)
        reasons = {r["reason"]: r["delta"] for r in billing.ledger_rows(self.ws2)}
        self.assertEqual(reasons["lead_business"], -3)
        self.assertEqual(reasons["lead_person"], -4)

    def test_grant_monthly_caps_rollover_at_one_grant(self):
        self._set_ws(self.ws2, plan="starter", status="active")
        con = lead_db.connect()
        try:
            with con.tx():
                billing.grant_monthly(con, self.ws2, "starter", "t1")
            # 25 trial credits carry (under the 200 cap) + 200 grant.
            self.assertEqual(billing.balance(self.ws2, con), 225)
            with con.tx():
                billing.grant_monthly(con, self.ws2, "starter", "t2")
            # 225 > 200: 25 expire first, then the fresh 200 lands.
            self.assertEqual(billing.balance(self.ws2, con), 400)
        finally:
            con.close()
        deltas = [r["delta"] for r in billing.ledger_rows(self.ws2)
                  if r["reason"] == "rollover_expiry"]
        self.assertEqual(deltas, [-25])

    def test_grant_monthly_scale_rolls_over_two_grants(self):
        """Scale's rollover_multiple is 2 (2026-07-23 tier perk): unused
        credits survive up to 2x the monthly grant before expiring."""
        self._set_ws(self.ws2, plan="scale", status="active")
        con = lead_db.connect()
        try:
            with con.tx():
                # Park the balance above 1x but under 2x the 1200 grant.
                billing.add_ledger(con, self.ws2, 1975, "adjustment", "seed")
            self.assertEqual(billing.balance(self.ws2, con), 2000)
            with con.tx():
                billing.grant_monthly(con, self.ws2, "scale", "t1")
            # 2000 <= 2400 cap: nothing expires, the grant lands on top.
            self.assertEqual(billing.balance(self.ws2, con), 3200)
            with con.tx():
                billing.grant_monthly(con, self.ws2, "scale", "t2")
            # 3200 > 2400: 800 expire first, then the fresh 1200 lands.
            self.assertEqual(billing.balance(self.ws2, con), 3600)
        finally:
            con.close()
        deltas = [r["delta"] for r in billing.ledger_rows(self.ws2, limit=50)
                  if r["reason"] == "rollover_expiry"]
        self.assertEqual(deltas, [-800])

    # --- the pre-run gate ----------------------------------------------------

    def test_run_guard_blocks_insufficient_credits(self):
        message = billing.run_guard(self.ws2, 26, None)
        self.assertIn("26", message)
        self.assertIn("25", message)
        # Persons cost double: 13 people = 26 credits > 25.
        self.assertIsNotNone(billing.run_guard(self.ws2, 13, "person"))
        self.assertIsNone(billing.run_guard(self.ws2, 25, None))
        self.assertIsNone(billing.run_guard(1, 500, None))  # internal: unmetered

    def test_run_guard_blocks_every_bad_status(self):
        self._set_ws(self.ws2, status="past_due")
        self.assertIn("payment issue", billing.run_guard(self.ws2, 1, None))
        self._set_ws(self.ws2, status="canceled")
        self.assertIn("ended", billing.run_guard(self.ws2, 1, None))
        self._set_ws(self.ws2, status="active", frozen=1)
        self.assertIn("frozen", billing.run_guard(self.ws2, 1, None))
        self._set_ws(self.ws2, frozen=0,
                     trial_expires_at="2026-01-01 00:00:00")
        self.assertIn("trial has ended", billing.run_guard(self.ws2, 1, None))

    def test_run_pipeline_raises_before_any_vendor_call(self):
        """The billing gate fires BEFORE the key preflight — with no vendor
        keys in the test env, anything past the gate would SystemExit
        instead, so the exception type proves the ordering."""
        from run_lead_gen import run_pipeline
        billing.debit_leads(self.ws2, 25, 0, ref="drain")
        with self.assertRaises(billing.BillingBlocked):
            run_pipeline("dentist", "Boise ID", 10, workspace_id=self.ws2)

    def test_enroll_and_create_campaign_blocked_when_not_active(self):
        from enroll_leads import create_campaign, enroll
        campaign_id = create_campaign(self.ws2, "C1", 30)  # active: fine
        self._set_ws(self.ws2, status="canceled")
        with self.assertRaises(billing.BillingBlocked):
            enroll(self.ws2, campaign_id, min_score=4, limit=10)
        with self.assertRaises(billing.BillingBlocked):
            create_campaign(self.ws2, "C2", 30)


class BillingWebhookTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self._secret_patch = mock.patch.dict(
            os.environ, {"DODO_WEBHOOK_SECRET": TEST_SECRET})
        self._secret_patch.start()
        self.ws2 = lead_db.create_workspace("Test Co")

    def tearDown(self) -> None:
        self._secret_patch.stop()
        self.teardown_tenancy()

    def _post(self, event_type: str, msg_id: str, **kwargs) -> tuple[int, str]:
        body = event_body(event_type, kwargs.pop("workspace_id", self.ws2),
                          **kwargs)
        return billing.process_webhook(signed_headers(body, msg_id), body)

    # --- signature -----------------------------------------------------------

    def test_signature_verifies_and_rejects_tampering(self):
        body = event_body("subscription.active", self.ws2)
        headers = signed_headers(body)
        self.assertTrue(billing.verify_webhook(headers, body))
        self.assertFalse(billing.verify_webhook(headers, body + b" "))
        stale = signed_headers(body, timestamp=int(time.time()) - 3600)
        self.assertFalse(billing.verify_webhook(stale, body))

    def test_missing_secret_rejects_everything(self):
        os.environ.pop("DODO_WEBHOOK_SECRET", None)
        body = event_body("subscription.active", self.ws2)
        status, message = billing.process_webhook(signed_headers(body), body)
        self.assertEqual(status, 400)

    # --- the four normalized events ------------------------------------------

    def test_activation_flips_plan_and_grants_credits(self):
        status, message = self._post("subscription.active", "evt_a1")
        self.assertEqual((status, "plan_activated" in message), (200, True))
        ws = lead_db.get_workspace(self.ws2)
        self.assertEqual(ws["plan"], "starter")
        self.assertEqual(ws["status"], "active")
        self.assertEqual(ws["provider_subscription_id"], "sub_123")
        self.assertIsNone(ws["trial_expires_at"])
        self.assertEqual(ws["credits_balance"], 25 + 200)

    def test_replay_of_the_same_event_is_a_no_op(self):
        self._post("subscription.active", "evt_r1")
        before = billing.balance(self.ws2)
        status, message = self._post("subscription.active", "evt_r1")
        self.assertEqual((status, message), (200, "duplicate ignored"))
        self.assertEqual(billing.balance(self.ws2), before)

    def test_payment_failure_pauses_and_renewal_recovers(self):
        self._post("subscription.active", "evt_p1")
        self._post("subscription.on_hold", "evt_p2")
        self.assertEqual(lead_db.get_workspace(self.ws2)["status"], "past_due")
        self._post("subscription.renewed", "evt_p3")
        ws = lead_db.get_workspace(self.ws2)
        self.assertEqual(ws["status"], "active")
        # 225 on hand > the 200 cap: 25 expired, 200 granted.
        self.assertEqual(ws["credits_balance"], 400)

    def test_cancellation_sets_canceled(self):
        self._post("subscription.active", "evt_c1")
        self._post("subscription.cancelled", "evt_c2")
        self.assertEqual(lead_db.get_workspace(self.ws2)["status"], "canceled")

    # --- routing edge cases ----------------------------------------------------

    def test_event_for_unknown_workspace_is_logged_not_lost(self):
        status, message = self._post("subscription.active", "evt_u1",
                                     workspace_id=999)
        self.assertEqual(status, 200)
        self.assertIn("no matching workspace", message)
        con = lead_db.connect()
        try:
            row = con.execute(
                "SELECT * FROM billing_events WHERE event_id = ?", ("evt_u1",)
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            con.close()

    def test_subscription_id_routes_events_without_metadata(self):
        self._post("subscription.active", "evt_s1")  # stores sub_123
        body = event_body("subscription.on_hold", None, with_metadata=False)
        status, _ = billing.process_webhook(
            signed_headers(body, "evt_s2"), body)
        self.assertEqual(status, 200)
        self.assertEqual(lead_db.get_workspace(self.ws2)["status"], "past_due")


if __name__ == "__main__":
    unittest.main()
