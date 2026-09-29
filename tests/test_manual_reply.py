"""Human-written 1:1 replies to a prospect who answered (in-app reply feature).

The product promise stays intact: a HUMAN types and sends every reply. This
suite pins the mechanics of that send — it goes through the workspace's own
mailbox, threads into the existing conversation, carries NO cold-email footer or
unsubscribe headers (the prospect started this thread), respects the
practice/live gate and suppression, and stops the automated follow-ups because a
human is now handling the lead.

Run: python -m unittest tests.test_manual_reply -v
"""
from __future__ import annotations

import os
import smtplib
import unittest
from unittest import mock

try:
    from tests.tenancy import TenancyTestMixin
except ImportError:
    from tenancy import TenancyTestMixin

import db
import lead_db
import mailboxes
import sequencer

PASSWORD = "tenant-app-pass-9021"


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL: records what would be sent, no sockets."""

    def __init__(self):
        self.sessions: list[dict] = []

    def __call__(self, host, port, timeout=None):
        outer = self
        session = {"host": host, "messages": []}

        class _Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def login(self_inner, user, password):
                session["user"] = user
                outer.sessions.append(session)

            def send_message(self_inner, msg):
                session["messages"].append(msg)

        return _Conn()

    @property
    def sent(self):
        return [m for s in self.sessions for m in s["messages"]]


class ManualReplyTest(TenancyTestMixin, unittest.TestCase):
    def setUp(self):
        self.setup_tenancy()
        self._key = mock.patch.dict(
            os.environ, {"MAILBOX_ENC_KEY": mailboxes.generate_key()})
        self._key.start()

    def tearDown(self):
        self._key.stop()
        self.teardown_tenancy()

    # --- scaffolding --------------------------------------------------------

    def _make_ws(self, name, plan="starter", live=True):
        ws_id = lead_db.create_workspace(name)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE workspaces SET plan=?, status='active', "
                    "live_sending=?, postal_address=? WHERE id=?",
                    (plan, 1 if live else 0, f"{name}, 1 Main St", ws_id))
        finally:
            con.close()
        return ws_id

    def _connect_box(self, ws_id, address="box@tenant.example"):
        domain = address.split("@")[1]
        mbx = mailboxes.add_mailbox(
            ws_id, address, PASSWORD, smtp_host=f"smtp.{domain}", smtp_port=465,
            imap_host=f"imap.{domain}", imap_port=993)
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE mailboxes SET status='active', warmup_started_at=? "
                    "WHERE id=? AND workspace_id=?",
                    (db.utcnow_plus(days=-30), mbx, ws_id))
        finally:
            con.close()
        return mbx

    def _seed_lead(self, ws_id, email="prospect@acme.example",
                   status="interested"):
        con = lead_db.connect()
        try:
            with con.tx():
                lead_id = con.insert_id(
                    "INSERT INTO leads (workspace_id, place_id, name, city, "
                    "email, email_verified, score, email_subject, email_body, "
                    "status, first_seen_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ws_id, f"pl-{ws_id}", "Acme Co", "Boise ID", email,
                     "mx_ok", 5, "quick question", "hi there", status,
                     db.utcnow(), db.utcnow()))
                campaign_id = con.insert_id(
                    "INSERT INTO campaigns (workspace_id, name, daily_cap, "
                    "status) VALUES (?, 'C', 30, 'active')", (ws_id,))
                # A step-1 send we already made — the thread anchor.
                con.execute(
                    "INSERT INTO sends (workspace_id, lead_id, campaign_id, "
                    "step_number, scheduled_at, sent_at, status, message_id) "
                    "VALUES (?,?,?,1,?,?,'sent','<orig-123@leadgen>')",
                    (ws_id, lead_id, campaign_id, db.utcnow(), db.utcnow()))
                # A pending day-3 follow-up that must be cancelled on reply.
                con.execute(
                    "INSERT INTO sends (workspace_id, lead_id, campaign_id, "
                    "step_number, scheduled_at, status) "
                    "VALUES (?,?,?,2,?, 'scheduled')",
                    (ws_id, lead_id, campaign_id, db.utcnow()))
        finally:
            con.close()
        return lead_id

    def _notes(self, ws_id, lead_id):
        con = lead_db.connect()
        try:
            return [dict(r) for r in con.execute(
                "SELECT body FROM notes WHERE workspace_id=? AND lead_id=?",
                (ws_id, lead_id)).fetchall()]
        finally:
            con.close()

    def _followups(self, ws_id, lead_id, status):
        con = lead_db.connect()
        try:
            return con.execute(
                "SELECT COUNT(*) n FROM sends WHERE workspace_id=? AND "
                "lead_id=? AND status=?", (ws_id, lead_id, status)).fetchone()["n"]
        finally:
            con.close()

    # --- the send happens ---------------------------------------------------

    def test_live_reply_sends_threaded_without_unsub_footer(self):
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            ws = self._make_ws("Acme Agency")
            self._connect_box(ws, "sender@agency.example")
            lead_id = self._seed_lead(ws)
            result = sequencer.send_manual_reply(
                ws, lead_id, "Thanks! Here are a few times that work.")

        self.assertTrue(result["sent"], result)
        self.assertTrue(result["live"])
        self.assertIsNone(result["error"])

        self.assertEqual(len(fake.sent), 1)
        msg = fake.sent[0]
        self.assertEqual(msg["To"], "prospect@acme.example")
        self.assertTrue(msg["Subject"].lower().startswith("re:"), msg["Subject"])
        self.assertEqual(msg["In-Reply-To"], "<orig-123@leadgen>")
        self.assertIsNone(msg["List-Unsubscribe"], "reply must not be bulk-tagged")
        content = msg.get_content().lower()
        self.assertNotIn("unsubscribe", content)
        self.assertNotIn("no thanks", content)
        self.assertIn("here are a few times", content)

        # recorded and follow-ups stopped
        self.assertTrue(any("Replied" in n["body"] for n in self._notes(ws, lead_id)))
        self.assertEqual(self._followups(ws, lead_id, "scheduled"), 0)
        self.assertEqual(self._followups(ws, lead_id, "cancelled"), 1)

    def test_practice_mode_does_not_send(self):
        fake = FakeSMTP()
        with mock.patch("smtplib.SMTP_SSL", fake):
            ws = self._make_ws("Trial Co", plan="trial", live=False)
            self._connect_box(ws)
            lead_id = self._seed_lead(ws)
            result = sequencer.send_manual_reply(ws, lead_id, "hello")

        self.assertFalse(result["live"])
        self.assertFalse(result["sent"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(fake.sent), 0, "practice mode must not open SMTP")

    def test_suppressed_lead_is_blocked(self):
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            ws = self._make_ws("Acme Agency")
            self._connect_box(ws)
            lead_id = self._seed_lead(ws, email="gone@acme.example")
            lead_db.add_suppression(ws, "gone@acme.example", "unsubscribe")
            result = sequencer.send_manual_reply(ws, lead_id, "hi again")

        self.assertIsNotNone(result["error"])
        self.assertFalse(result["sent"])
        self.assertEqual(len(fake.sent), 0)

    def test_blank_body_sends_nothing(self):
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            ws = self._make_ws("Acme Agency")
            self._connect_box(ws)
            lead_id = self._seed_lead(ws)
            result = sequencer.send_manual_reply(ws, lead_id, "   ")

        self.assertIsNotNone(result["error"])
        self.assertEqual(len(fake.sent), 0)

    def test_other_workspace_cannot_reply(self):
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            ws_a = self._make_ws("A Co")
            self._connect_box(ws_a, "a@a.example")
            lead_a = self._seed_lead(ws_a)
            ws_b = self._make_ws("B Co")
            result = sequencer.send_manual_reply(ws_b, lead_a, "sneaky")

        self.assertIsNotNone(result["error"])
        self.assertEqual(len(fake.sent), 0)

    def test_no_mailbox_connected(self):
        fake = FakeSMTP()
        with mock.patch.dict(os.environ, {"DRY_RUN": "false"}), \
                mock.patch("smtplib.SMTP_SSL", fake):
            ws = self._make_ws("Acme Agency")  # live, but no mailbox connected
            lead_id = self._seed_lead(ws)
            result = sequencer.send_manual_reply(ws, lead_id, "hi")

        self.assertIsNotNone(result["error"])
        self.assertEqual(len(fake.sent), 0)

    def test_practice_tenant_no_mailbox_gives_clear_error(self):
        """A practice-mode tenant (not owner) with no connected mailbox gets the
        'rehearsal' pseudo-box from _workspace_boxes. It must be stripped so the
        human sees the plain 'connect a mailbox' message — not a NoneType crash
        from dereferencing the pseudo-box's empty row."""
        fake = FakeSMTP()
        with mock.patch("smtplib.SMTP_SSL", fake):
            ws = self._make_ws("Trial Co", plan="trial", live=False)
            lead_id = self._seed_lead(ws)
            result = sequencer.send_manual_reply(ws, lead_id, "hi there")

        self.assertIsNotNone(result["error"])
        self.assertIn("mailbox", result["error"].lower())
        self.assertNotIn("nonetype", result["error"].lower())
        self.assertEqual(len(fake.sent), 0)


if __name__ == "__main__":
    unittest.main()
