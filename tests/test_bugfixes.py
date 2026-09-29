"""Regression tests for the bug pass of 2026-09-29, one per fixed bug.

Run (from the repo root):
    python -m unittest tests.test_bugfixes -v
"""
from __future__ import annotations

import base64
import json
import smtplib
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp
import web.jobs as jobs

try:
    from tests.tenancy import TenancyTestMixin, signup_owner, signup_tenant
    from tests.test_billing import TEST_SECRET, event_body, signed_headers
except ImportError:
    from tenancy import TenancyTestMixin, signup_owner, signup_tenant
    from test_billing import TEST_SECRET, event_body, signed_headers

import billing
import mailboxes
import outreach_tick
import process_replies
import read_inbox
import send_email
import sequencer

lead_db = webapp.lead_db


class RedirectAndLinkTestCase(unittest.TestCase):
    def test_back_targets_cannot_leave_the_site(self):
        for bad in ("//evil.example", "/\\evil.example", "/\tevil.example",
                    "https://evil.example", ""):
            self.assertEqual(webapp._safe_back(bad, "/pipeline"), "/pipeline")
        self.assertEqual(webapp._safe_back("/queue?x=1", "/pipeline"), "/queue?x=1")

    def test_scraped_links_must_be_web_addresses(self):
        self.assertEqual(webapp._safe_href("javascript:alert(1)"), "#")
        self.assertEqual(webapp._safe_href(None), "#")
        self.assertEqual(webapp._safe_href("https://a.example/x"),
                         "https://a.example/x")

    def test_unsubscribe_token_with_odd_characters_is_just_invalid(self):
        raw = "1:1:sig-é".encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertIsNone(send_email.verify_unsub_token(token))


class JobsTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self._jobs = dict(jobs.JOBS)
        jobs.JOBS.clear()
        self.client = TestClient(webapp.app)
        signup_owner(self.client)

    def tearDown(self) -> None:
        jobs.JOBS.clear()
        jobs.JOBS.update(self._jobs)
        self.teardown_tenancy()

    def _job(self, job_id: str, status: str, workspace_id: int = 1, **kw):
        job = jobs.Job(id=job_id, workspace_id=workspace_id, category="dentist",
                       city="Austin TX", limit=5, status=status, **kw)
        jobs.JOBS[job_id] = job
        return job

    def test_json_status_route_is_reachable(self):
        self._job("abc", "running")
        r = self.client.get("/jobs/abc.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "running")
        self.assertEqual(self.client.get("/jobs/nope.json").status_code, 404)

    def test_gate_cannot_start_a_second_run_in_the_same_workspace(self):
        self._job("gated", "awaiting_choice")
        self._job("busy", "running")
        self.assertIsNone(jobs.claim_gate("gated", 1, "enrich"))
        self.assertEqual(jobs.JOBS["gated"].status, "awaiting_choice")
        # Another workspace's run is no obstacle.
        jobs.JOBS["busy"].workspace_id = 2
        self.assertIsNotNone(jobs.claim_gate("gated", 1, "enrich"))

    def test_day_old_finished_jobs_are_dropped(self):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        self._job("old", "done", finished_at=old)
        self._job("fresh", "done", finished_at=datetime.now(timezone.utc))
        self._job("live", "running")
        with jobs._lock:
            jobs._expire_stale_gates()
        self.assertEqual(sorted(jobs.JOBS), ["fresh", "live"])

    def test_huge_page_number_does_not_crash(self):
        self.assertEqual(self.client.get(
            "/pipeline?page=999999999999999999").status_code, 200)

    def test_deleted_offer_cannot_be_edited(self):
        offer_id = lead_db.create_offer(1, {
            "name": "Old", "product_description": "d", "value_prop": "v"})
        lead_db.delete_offer(1, offer_id)
        r = self.client.post(f"/offers/{offer_id}/update", data={
            "name": "Revived", "product_description": "d", "value_prop": "v"})
        self.assertEqual(r.status_code, 404)

    def test_blocked_workspace_cannot_start_a_paid_dm(self):
        tenant = TestClient(webapp.app)
        signup_tenant(tenant)
        ws_id = lead_db.get_user_by_email("tenant@test-co.example")["workspace_id"]
        lead_db.upsert_leads(ws_id, [{
            "name": "DM Dental", "place_id": "d1",
            "social_links": {"facebook": "https://facebook.com/dm"}}])
        lead_id = lead_db.get_leads(ws_id)[0]["id"]
        billing.set_frozen(ws_id, True)
        with mock.patch.object(webapp.write_dm, "write_dm") as write:
            r = tenant.post(f"/dms/{lead_id}/write")
        self.assertEqual(r.status_code, 402)
        write.assert_not_called()


class BillingWebhookTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        patcher = mock.patch.dict("os.environ", {
            "DODO_WEBHOOK_SECRET": TEST_SECRET,
            "DODO_PRODUCT_STARTER": "prod_starter",
            "DODO_PRODUCT_SCALE": "prod_scale"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws = lead_db.create_workspace("Buyer Co")

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def _send(self, body: bytes, msg_id: str):
        return billing.process_webhook(signed_headers(body, msg_id), body)

    def _event(self, metadata: dict, product: str = "prod_starter",
               sub_id: str = "sub_1", kind: str = "subscription.active") -> bytes:
        return json.dumps({"id": "p", "type": kind, "data": {
            "subscription_id": sub_id, "product_id": product,
            "customer": {"customer_id": "cus_1"}, "metadata": metadata}}).encode()

    def test_edited_checkout_link_cannot_upgrade_the_plan(self):
        meta = {"workspace_id": str(self.ws), "plan": "scale",
                "sig": billing.metadata_sig(self.ws, "starter")}  # plan edited
        self._send(self._event(meta, product="prod_unknown"), "evt_a")
        self.assertEqual(lead_db.get_workspace(self.ws)["plan"], "trial")

    def test_paid_product_decides_the_plan(self):
        meta = {"workspace_id": str(self.ws), "plan": "scale",
                "sig": billing.metadata_sig(self.ws, "scale")}
        self._send(self._event(meta, product="prod_starter"), "evt_b")
        self.assertEqual(lead_db.get_workspace(self.ws)["plan"], "starter")

    def test_unsigned_metadata_cannot_point_at_another_workspace(self):
        victim = lead_db.create_workspace("Victim Co")
        self._send(self._event({"workspace_id": str(victim), "plan": "starter"},
                               kind="subscription.cancelled"), "evt_c")
        self.assertEqual(lead_db.get_workspace(victim)["status"], "active")

    def test_owner_workspace_is_never_billed(self):
        meta = {"workspace_id": "1", "plan": "starter",
                "sig": billing.metadata_sig(1, "starter")}
        self._send(self._event(meta), "evt_d")
        self.assertEqual(lead_db.get_workspace(1)["plan"], "internal")

    def test_late_renewal_does_not_revive_a_canceled_plan(self):
        self._send(event_body("subscription.active", self.ws, sub_id="sub_9"),
                   "evt_e")
        self._send(event_body("subscription.cancelled", self.ws, sub_id="sub_9"),
                   "evt_f")
        before = billing.balance(self.ws)
        self._send(event_body("subscription.renewed", self.ws, sub_id="sub_9"),
                   "evt_g")
        self.assertEqual(lead_db.get_workspace(self.ws)["status"], "canceled")
        self.assertEqual(billing.balance(self.ws), before)


class TickAndRepliesTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_nothing_is_sent_when_replies_could_not_be_read(self):
        send = mock.Mock(return_value={"sent": 0})
        with mock.patch.object(outreach_tick, "poll_all",
                               side_effect=RuntimeError("imap down")), \
                mock.patch.object(outreach_tick, "process_pending",
                                  return_value={}), \
                mock.patch.object(outreach_tick.sequencer, "tick", send), \
                mock.patch.object(outreach_tick, "sync_changed_leads",
                                  return_value=0), \
                mock.patch.object(outreach_tick.daily_digest, "maybe_send",
                                  return_value=None), \
                mock.patch.object(outreach_tick.notify, "send_alert"):
            results = outreach_tick.run_once()
        send.assert_not_called()
        self.assertTrue(str(results["send due emails"]).startswith("SKIPPED"))

    def test_delay_notice_is_not_a_bounce(self):
        self.assertFalse(read_inbox.is_bounce(
            "mailer-daemon@googlemail.com",
            "Delivery Status Notification (Delay)", ""))
        self.assertTrue(read_inbox.is_bounce(
            "mailer-daemon@googlemail.com",
            "Delivery Status Notification (Failure)", ""))

    def test_unreadable_mailbox_login_does_not_stop_the_poll(self):
        row = {"id": 7, "workspace_id": 1, "address": "box@t.example",
               "creds_encrypted": "garbage", "imap_host": "h", "imap_port": 993,
               "created_at": "2026-01-01 00:00:00"}
        with mock.patch.object(read_inbox.mailboxes, "decrypt_creds",
                               side_effect=ValueError("bad token")), \
                mock.patch.object(read_inbox.mailboxes, "mark_error") as mark, \
                mock.patch.object(read_inbox.notify, "send_alert"):
            self.assertEqual(read_inbox.poll_workspace_mailbox(row), 0)
        mark.assert_called_once()

    def _reply(self, body: str = "not me, sorry") -> int:
        lead_db.upsert_leads(1, [{"name": "Acme", "place_id": "a1",
                                  "email": "a@acme.example",
                                  "email_verified": "mx_ok"}])
        lead = lead_db.get_leads(1)[0]
        lead_db.set_status(1, lead["id"], "contacted")
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "INSERT INTO replies (workspace_id, lead_id, mailbox, "
                    "from_email, subject, body_text, received_at) "
                    "VALUES (1, ?, 'me@x.example', 'a@acme.example', 'Re: hi', "
                    "?, '2026-09-01 10:00:00')", (lead["id"], body))
        finally:
            con.close()
        return lead["id"]

    def test_unsure_wrong_person_goes_to_a_human(self):
        lead_id = self._reply()
        verdict = {"classification": "wrong_person", "confidence": "low",
                   "summary": "maybe the wrong person", "suggested_action": ""}
        with mock.patch.object(process_replies.notify, "send_batch"):
            process_replies.process_pending(classifier=lambda _: verdict)
        self.assertEqual(lead_db.get_lead(1, lead_id)["status"], "needs_human")
        self.assertFalse(lead_db.is_suppressed(1, "a@acme.example"))

    def test_alerts_still_go_out_when_a_later_reply_crashes(self):
        self._reply("yes please, call me")
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "INSERT INTO replies (workspace_id, mailbox, from_email, "
                    "subject, body_text, received_at) VALUES (1, 'me@x.example', "
                    "'b@b.example', 'Re: hi', 'boom', '2026-09-01 11:00:00')")
        finally:
            con.close()

        def classifier(item):
            if item["reply_text"] == "boom":
                raise RuntimeError("network down")
            return {"classification": "interested", "confidence": "high",
                    "summary": "wants a call", "suggested_action": "call"}

        with mock.patch.object(process_replies.notify, "send_batch") as batch:
            with self.assertRaises(RuntimeError):
                process_replies.process_pending(classifier=classifier)
        batch.assert_called_once()
        self.assertEqual(batch.call_args[0][1][0]["kind"], "interested")


class MailServerTestCase(unittest.TestCase):
    def test_mail_server_must_be_public_and_on_a_mail_port(self):
        check = mailboxes.check_server
        check("smtp.gmail.com", 465, mailboxes.SMTP_PORTS)  # unresolved in tests
        for host in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "[::1]"):
            with self.assertRaises(ValueError):
                check(host, 465, mailboxes.SMTP_PORTS)
        with self.assertRaises(ValueError):
            check("smtp.gmail.com", 6379, mailboxes.SMTP_PORTS)
        with mock.patch.object(mailboxes, "_resolve", return_value=["192.168.1.9"]):
            with self.assertRaises(ValueError):
                check("sneaky.example", 993, mailboxes.IMAP_PORTS)
        with mock.patch.object(mailboxes, "_resolve", return_value=["142.250.1.1"]):
            check("imap.ok.example", 993, mailboxes.IMAP_PORTS)


class SendFailureTestCase(unittest.TestCase):
    def test_only_mailbox_trouble_counts_as_a_mailbox_failure(self):
        mailbox = sequencer._is_mailbox_failure
        self.assertTrue(mailbox(smtplib.SMTPAuthenticationError(535, b"bad")))
        self.assertTrue(mailbox(smtplib.SMTPServerDisconnected("gone")))
        self.assertTrue(mailbox(TimeoutError("timed out")))
        self.assertFalse(mailbox(smtplib.SMTPRecipientsRefused({"x": (550, b"no")})))
        self.assertFalse(mailbox(smtplib.SMTPDataError(554, b"rejected")))
        self.assertFalse(mailbox(ValueError("newline in subject")))
        self.assertFalse(mailbox(RuntimeError("postal address missing")))


if __name__ == "__main__":
    unittest.main()
