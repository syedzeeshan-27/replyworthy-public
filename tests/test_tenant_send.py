"""Stage-6 send/read loop tests (plans/v2/06 E): two workspaces send
independently from their OWN mailboxes, the warmup ramp caps a tick, the
per-workspace practice/live gate (and the global DRY_RUN master override),
the bounce breaker pauses one workspace only, a broken mailbox flips to
error and alerts the customer, and tenant-mailbox replies never route into
another workspace. All offline: fake SMTP, no sockets.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import os
import smtplib
import unittest
from datetime import datetime, timezone
from unittest import mock

try:
    from tests.tenancy import TenancyTestMixin
except ImportError:
    from tenancy import TenancyTestMixin

import db
import lead_db
import mailboxes
import process_replies
import sequencer
from enroll_leads import create_campaign, enroll
from read_inbox import store_message

PASSWORD = "tenant-app-pass-3311"

# Wednesday 18:00 UTC = mid-morning across US zones (inside the send window).
IN_WINDOW = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)

LEAD = {
    "name": "Window Dental", "city": "Boise ID",
    "email_verified": "mx_ok", "score": 5,
    "email_subject": "hi", "email_body": "hello body",
}


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL: records sessions, never opens sockets."""

    def __init__(self):
        self.sessions: list[dict] = []
        self.fail_auth = False

    def __call__(self, host, port, timeout=None):
        outer = self
        session = {"host": host, "port": port, "user": None, "messages": []}

        class _Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def login(self_inner, user, password):
                if outer.fail_auth:
                    raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
                session["user"] = user
                outer.sessions.append(session)

            def send_message(self_inner, msg):
                session["messages"].append(msg)

        return _Conn()


class TenantSendTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self._key_patch = mock.patch.dict(
            os.environ, {"MAILBOX_ENC_KEY": mailboxes.generate_key()})
        self._key_patch.start()
        # Alerts must never leave the machine in tests: pretend Telegram
        # always delivers so no email fallback is attempted.
        self._tg_patch = mock.patch("notify._telegram_send", return_value=True)
        self._tg_patch.start()

    def tearDown(self) -> None:
        self._tg_patch.stop()
        self._key_patch.stop()
        self.teardown_tenancy()

    # --- scaffolding -------------------------------------------------------------

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

    def _make_ws(self, name: str, live: bool = True) -> int:
        ws_id = lead_db.create_workspace(name)
        self._set_ws(ws_id, plan="starter", status="active",
                     live_sending=1 if live else 0,
                     postal_address=f"{name}, 1 Main St, Boise ID 83702")
        return ws_id

    def _connect_box(self, ws_id: int, address: str, days_old: int = 30,
                     cap: int = 50) -> int:
        domain = address.split("@")[1]
        mailbox_id = mailboxes.add_mailbox(
            ws_id, address, PASSWORD,
            smtp_host=f"smtp.{domain}", smtp_port=465,
            imap_host=f"imap.{domain}", imap_port=993)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE mailboxes SET status = 'active', "
                    "warmup_started_at = ?, daily_cap = ? "
                    "WHERE id = ? AND workspace_id = ?",
                    (db.utcnow_plus(days=-days_old), cap, mailbox_id, ws_id))
        finally:
            con.close()
        return mailbox_id

    def _enroll(self, ws_id: int, emails: list[str], tag: str) -> int:
        leads = [dict(LEAD, name=f"Biz {tag}{i}", place_id=f"{tag}-{i}",
                      email=email) for i, email in enumerate(emails)]
        lead_db.upsert_leads(ws_id, leads)
        campaign_id = create_campaign(ws_id, f"C-{tag}", 30)
        enroll(ws_id, campaign_id, min_score=4, limit=50)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE sends SET scheduled_at = ? WHERE workspace_id = ?",
                    ("2026-07-01 00:00:00", ws_id))
        finally:
            con.close()
        return campaign_id

    def _sends(self, ws_id: int) -> list[dict]:
        con = lead_db.connect()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM sends WHERE workspace_id = ? ORDER BY id",
                (ws_id,)).fetchall()]
        finally:
            con.close()

    # --- independent per-workspace sending ------------------------------------------

    def test_each_workspace_sends_live_from_its_own_mailbox(self):
        ws_a, ws_b = self._make_ws("A Co"), self._make_ws("B Co")
        self._connect_box(ws_a, "sales@a-co.example")
        self._connect_box(ws_b, "hello@b-co.example")
        self._enroll(ws_a, ["p1@prospect.example"], "a")
        self._enroll(ws_b, ["p2@prospect.example"], "b")

        fake = FakeSMTP()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)

        self.assertEqual(counters["sent"], 2)
        self.assertEqual(self._sends(ws_a)[0]["mailbox"], "sales@a-co.example")
        self.assertEqual(self._sends(ws_b)[0]["mailbox"], "hello@b-co.example")
        hosts = sorted(s["host"] for s in fake.sessions)
        self.assertEqual(hosts, ["smtp.a-co.example", "smtp.b-co.example"])
        # The message really is the tenant's: their From, their footer.
        for session in fake.sessions:
            msg = session["messages"][0]
            body = msg.get_content()
            self.assertIn("List-Unsubscribe", msg)
            if session["host"] == "smtp.a-co.example":
                self.assertEqual(msg["From"], "sales@a-co.example")
                self.assertIn("A Co, 1 Main St", body)
                self.assertNotIn("B Co", body)
            else:
                self.assertEqual(msg["From"], "hello@b-co.example")
                self.assertIn("B Co, 1 Main St", body)
        # Plan E: credentials never in logs.
        self.assertNotIn(PASSWORD, out.getvalue() + err.getvalue())

    def test_practice_workspace_never_touches_smtp(self):
        ws_a = self._make_ws("A Co", live=True)
        ws_b = self._make_ws("B Co", live=False)  # practice mode
        self._connect_box(ws_a, "sales@a-co.example")
        self._connect_box(ws_b, "hello@b-co.example")
        self._enroll(ws_a, ["p1@prospect.example"], "a")
        self._enroll(ws_b, ["p2@prospect.example"], "b")

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)

        self.assertEqual(counters["sent"], 2)  # B rehearses, A sends
        self.assertEqual(len(fake.sessions), 1)
        self.assertEqual(fake.sessions[0]["host"], "smtp.a-co.example")
        self.assertTrue(
            self._sends(ws_b)[0]["message_id"].startswith("<practice-"))

    def test_trial_plan_can_never_send_live(self):
        ws_t = lead_db.create_workspace("Trial Co")
        # Even with the flags forced on, trial stays practice-only.
        self._set_ws(ws_t, live_sending=1,
                     postal_address="Trial Co, 1 Main St, Boise ID")
        self._connect_box(ws_t, "trial@t-co.example")
        self._enroll(ws_t, ["p3@prospect.example"], "t")

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)

        self.assertEqual(counters["sent"], 1)
        self.assertEqual(fake.sessions, [])
        self.assertTrue(
            self._sends(ws_t)[0]["message_id"].startswith("<practice-"))

    def test_global_dry_run_is_the_master_override(self):
        ws_a = self._make_ws("A Co", live=True)
        self._connect_box(ws_a, "sales@a-co.example")
        self._enroll(ws_a, ["p1@prospect.example"], "a")

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "true"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)

        self.assertEqual(counters["sent"], 1)   # processed, but only rehearsed
        self.assertEqual(fake.sessions, [])

    # --- warmup ramp ---------------------------------------------------------------

    def test_warmup_ramp_caps_a_tick(self):
        ws_a = self._make_ws("A Co")
        self._connect_box(ws_a, "sales@a-co.example", days_old=0, cap=50)
        self._enroll(ws_a, [f"p{i}@prospect.example" for i in range(7)], "a")

        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)

        self.assertEqual(counters["sent"], 5)        # week-1 ramp: 5/day
        self.assertEqual(counters["skipped_cap"], 2)

    # --- per-workspace breaker --------------------------------------------------------

    def test_breaker_pauses_one_workspace_and_spares_the_other(self):
        ws_a = self._make_ws("A Co", live=False)
        ws_b = self._make_ws("B Co", live=False)
        # History: 12 sent emails each; every one of A's bounced.
        campaigns = {ws_id: create_campaign(ws_id, "hist", 30)
                     for ws_id in (ws_a, ws_b)}
        con = lead_db.connect()
        try:
            with con.tx():
                for ws_id, bounced in ((ws_a, True), (ws_b, False)):
                    campaign = campaigns[ws_id]
                    for i in range(12):
                        lead_id = con.insert_id(
                            "INSERT INTO leads (workspace_id, name, email, "
                            "email_verified, status, first_seen_at, updated_at) "
                            "VALUES (?, ?, ?, ?, 'contacted', ?, ?)",
                            (ws_id, f"L{i}", f"l{i}@{ws_id}.example",
                             "bounced" if bounced else "mx_ok",
                             db.utcnow(), db.utcnow()))
                        con.execute(
                            "INSERT INTO sends (workspace_id, lead_id, "
                            "campaign_id, step_number, scheduled_at, sent_at, "
                            "status) VALUES (?, ?, ?, 1, ?, ?, 'sent')",
                            (ws_id, lead_id, campaign,
                             db.utcnow(), db.utcnow()))
        finally:
            con.close()

        counters = process_replies.process_pending(
            classifier=lambda payload: {"classification": "needs_human",
                                        "confidence": "low", "summary": "",
                                        "suggested_action": ""})
        self.assertTrue(counters["auto_paused"])
        self.assertEqual(lead_db.get_workspace(ws_a)["outreach_paused"], 1)
        self.assertEqual(lead_db.get_workspace(ws_b)["outreach_paused"], 0)

        # A's due sends wait; B keeps sending (practice mode here).
        self._enroll(ws_a, ["pa@prospect.example"], "a2")
        self._enroll(ws_b, ["pb@prospect.example"], "b2")
        tick = sequencer.tick(now_utc=IN_WINDOW, spacing=None)
        self.assertGreaterEqual(tick["skipped_paused"], 1)
        sent_b = [s for s in self._sends(ws_b) if s["status"] == "sent"
                  and s["mailbox"] == "dryrun@localhost"]
        self.assertEqual(len(sent_b), 1)
        scheduled_a = [s for s in self._sends(ws_a) if s["status"] == "scheduled"]
        self.assertEqual(len(scheduled_a), 1)  # untouched, resumes later

    # --- broken mailbox -----------------------------------------------------------------

    def test_broken_mailbox_flips_to_error_and_alerts_the_customer(self):
        ws_a = self._make_ws("A Co")
        self._set_ws(ws_a, alert_email="alerts@a-co.example")
        mailbox_id = self._connect_box(ws_a, "sales@a-co.example")
        self._enroll(ws_a, ["p1@prospect.example"], "a")

        fake = FakeSMTP()
        fake.fail_auth = True
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake), \
                mock.patch("send_email.send_system_email",
                           return_value=True) as system_mail:
            counters = sequencer.tick(now_utc=IN_WINDOW, spacing=None)

        self.assertEqual(counters["failed"], 1)
        row = mailboxes.get_mailbox(ws_a, mailbox_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("535", row["last_error"])
        self.assertNotIn(PASSWORD, row["last_error"])
        # The CUSTOMER was told, on their own alert address.
        system_mail.assert_called()
        self.assertEqual(system_mail.call_args.args[0], "alerts@a-co.example")
        con = lead_db.connect()
        try:
            alert = con.execute(
                "SELECT * FROM alerts WHERE workspace_id = ? AND kind = ?",
                (ws_a, f"mailbox_error_{mailbox_id}")).fetchone()
            self.assertIsNotNone(alert)
        finally:
            con.close()

    # --- reply routing stays inside the mailbox's workspace ------------------------------

    def test_tenant_mailbox_replies_never_cross_workspaces(self):
        ws_a, ws_b = self._make_ws("A Co"), self._make_ws("B Co")
        prospect = "shared@prospect.example"
        campaigns = {ws_id: create_campaign(ws_id, "hist", 30)
                     for ws_id in (ws_a, ws_b)}
        con = lead_db.connect()
        try:
            with con.tx():
                for ws_id, mid in ((ws_a, "<mid-a@x>"), (ws_b, "<mid-b@x>")):
                    campaign = campaigns[ws_id]
                    lead_id = con.insert_id(
                        "INSERT INTO leads (workspace_id, name, email, "
                        "email_verified, status, email_subject, "
                        "first_seen_at, updated_at) "
                        "VALUES (?, 'Shared Biz', ?, 'mx_ok', 'contacted', "
                        "'hi', ?, ?)",
                        (ws_id, prospect, db.utcnow(), db.utcnow()))
                    con.execute(
                        "INSERT INTO sends (workspace_id, lead_id, campaign_id, "
                        "step_number, scheduled_at, sent_at, status, "
                        "message_id) VALUES (?, ?, ?, 1, ?, ?, 'sent', ?)",
                        (ws_id, lead_id, campaign, db.utcnow(), db.utcnow(),
                         mid))

            # A reply lands in B's OWN mailbox: it must resolve to B's lead,
            # even though A knows the same prospect address.
            with con.tx():
                inserted = store_message(
                    con, "hello@b-co.example", "INBOX", "101",
                    from_email=prospect, subject="Re: hi", body="yes please",
                    received_at=db.utcnow(), scope_workspace_id=ws_b)
            self.assertTrue(inserted)
            reply = con.execute(
                "SELECT * FROM replies WHERE imap_uid = ?",
                ("INBOX:101",)).fetchone()
            self.assertEqual(reply["workspace_id"], ws_b)

            # Even a thread header pointing at A's send cannot pull the reply
            # into A when it arrived at B's mailbox.
            with con.tx():
                store_message(
                    con, "hello@b-co.example", "INBOX", "102",
                    from_email="stranger@elsewhere.example", subject="huh",
                    body="?", received_at=db.utcnow(),
                    in_reply_to="<mid-a@x>", scope_workspace_id=ws_b)
            reply = con.execute(
                "SELECT * FROM replies WHERE imap_uid = ?",
                ("INBOX:102",)).fetchone()
            self.assertEqual(reply["workspace_id"], ws_b)
            self.assertIsNone(reply["lead_id"])
            self.assertEqual(reply["classification"], "needs_human")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
