"""Stage-6 mailbox module tests (plans/v2/06 E): credential encryption
round-trip (and creds never in logs), the connect verification state machine
on fake transports, the warmup ramp curve + cap clamp, and the key-rotation
script. All offline — no SMTP/IMAP socket is ever opened.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
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

PASSWORD = "super-secret-app-pass-9911"


class FakeSmtpSend:
    """Records the verification self-send instead of talking to a server."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.sent: list[dict] = []

    def __call__(self, host, port, username, password, msg):
        if self.fail:
            raise self.fail
        self.sent.append({"host": host, "port": port, "username": username,
                          "subject": msg["Subject"]})


class MailboxesTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.key = mailboxes.generate_key()
        self._key_patch = mock.patch.dict(
            os.environ, {"MAILBOX_ENC_KEY": self.key})
        self._key_patch.start()
        self.ws = lead_db.create_workspace("Test Co")

    def tearDown(self) -> None:
        self._key_patch.stop()
        self.teardown_tenancy()

    def _add(self, address="sales@test-co.example", password=PASSWORD) -> int:
        return mailboxes.add_mailbox(
            self.ws, address, password,
            smtp_host="smtp.test.example", smtp_port=465,
            imap_host="imap.test.example", imap_port=993)

    def _verify_ok(self, mailbox_id: int):
        """Run verification against a fake SMTP+IMAP pair where the
        self-send really 'arrives'."""
        smtp = FakeSmtpSend()

        def imap_subjects(host, port, username, password):
            return [s["subject"] for s in smtp.sent]

        return mailboxes.verify_mailbox(
            self.ws, mailbox_id, smtp_send=smtp,
            imap_subjects=imap_subjects, sleep=lambda s: None)

    # --- encryption ------------------------------------------------------------

    def test_creds_encrypt_roundtrip(self):
        token = mailboxes.encrypt_creds("user@x.example", PASSWORD)
        self.assertNotIn(PASSWORD, token)
        creds = mailboxes.decrypt_creds(token)
        self.assertEqual(creds, {"username": "user@x.example",
                                 "password": PASSWORD})

    def test_stored_creds_are_not_plaintext(self):
        mailbox_id = self._add()
        row = mailboxes.get_mailbox(self.ws, mailbox_id)
        self.assertNotIn(PASSWORD, row["creds_encrypted"])
        self.assertEqual(
            mailboxes.decrypt_creds(row["creds_encrypted"])["password"],
            PASSWORD)

    def test_list_mailboxes_never_returns_creds(self):
        self._add()
        rows = mailboxes.list_mailboxes(self.ws)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("creds_encrypted", rows[0])
        self.assertEqual(rows[0]["status"], "unverified")

    def test_add_validates_address_and_password(self):
        with self.assertRaises(ValueError):
            self._add(address="not-an-address")
        with self.assertRaises(ValueError):
            self._add(password="   ")

    def test_delete_removes_the_stored_creds(self):
        mailbox_id = self._add()
        mailboxes.delete_mailbox(self.ws, mailbox_id)
        self.assertIsNone(mailboxes.get_mailbox(self.ws, mailbox_id))

    # --- verification state machine ---------------------------------------------

    def test_verify_success_goes_active_and_anchors_warmup(self):
        mailbox_id = self._add()
        ok, message = self._verify_ok(mailbox_id)
        self.assertTrue(ok, message)
        row = mailboxes.get_mailbox(self.ws, mailbox_id)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["last_error"])
        self.assertIsNotNone(row["warmup_started_at"])

    def test_verify_wrong_password_goes_error_with_plain_words(self):
        mailbox_id = self._add()
        smtp = FakeSmtpSend(
            fail=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
        ok, message = mailboxes.verify_mailbox(
            self.ws, mailbox_id, smtp_send=smtp,
            imap_subjects=lambda *a: [], sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("app password", message)
        self.assertNotIn(PASSWORD, message)
        row = mailboxes.get_mailbox(self.ws, mailbox_id)
        self.assertEqual(row["status"], "error")
        self.assertIn("app password", row["last_error"])
        self.assertNotIn(PASSWORD, row["last_error"])

    def test_verify_imap_failure_goes_error(self):
        mailbox_id = self._add()
        smtp = FakeSmtpSend()

        def imap_fails(*a):
            raise ConnectionError("connection refused")

        ok, message = mailboxes.verify_mailbox(
            self.ws, mailbox_id, smtp_send=smtp,
            imap_subjects=imap_fails, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("IMAP", message)
        self.assertEqual(
            mailboxes.get_mailbox(self.ws, mailbox_id)["status"], "error")

    def test_verify_readback_missing_goes_error(self):
        mailbox_id = self._add()
        smtp = FakeSmtpSend()
        ok, message = mailboxes.verify_mailbox(
            self.ws, mailbox_id, smtp_send=smtp,
            imap_subjects=lambda *a: ["unrelated subject"],
            sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("never showed up", message)

    def test_reverify_after_fix_recovers_to_active(self):
        mailbox_id = self._add()
        smtp = FakeSmtpSend(
            fail=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
        mailboxes.verify_mailbox(self.ws, mailbox_id, smtp_send=smtp,
                                 imap_subjects=lambda *a: [],
                                 sleep=lambda s: None)
        ok, _ = self._verify_ok(mailbox_id)
        self.assertTrue(ok)
        row = mailboxes.get_mailbox(self.ws, mailbox_id)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["last_error"])

    def test_creds_never_appear_in_logs(self):
        """Plan E: grep the logs to prove it — everything the module prints
        during add + failed verify + successful verify is password-free."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mailbox_id = self._add()
            smtp = FakeSmtpSend(
                fail=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
            mailboxes.verify_mailbox(self.ws, mailbox_id, smtp_send=smtp,
                                     imap_subjects=lambda *a: [],
                                     sleep=lambda s: None)
            self._verify_ok(mailbox_id)
        logged = out.getvalue() + err.getvalue()
        self.assertNotIn(PASSWORD, logged)

    # --- warmup ramp -------------------------------------------------------------

    def test_ramp_curve(self):
        anchor = "2026-07-01 00:00:00"
        cases = (
            ("2026-07-01 12:00:00", 5),    # day 0
            ("2026-07-07 23:00:00", 5),    # day 6
            ("2026-07-08 01:00:00", 10),   # day 7
            ("2026-07-14 12:00:00", 10),   # day 13
            ("2026-07-15 01:00:00", 20),   # day 14
            ("2026-07-21 23:00:00", 20),   # day 20
            ("2026-07-22 01:00:00", None),  # day 21: ramp over
        )
        for now, expected in cases:
            self.assertEqual(mailboxes.ramp_cap(anchor, now), expected, now)
        # No anchor yet (unverified) = brand new = week-1 cap.
        self.assertEqual(mailboxes.ramp_cap(None), 5)

    def test_effective_cap_is_min_of_customer_cap_and_ramp(self):
        fresh = {"daily_cap": 50, "warmup_started_at": None}
        self.assertEqual(mailboxes.effective_cap(fresh), 5)
        seasoned = {"daily_cap": 50,
                    "warmup_started_at": db.utcnow_plus(days=-40)}
        self.assertEqual(mailboxes.effective_cap(seasoned), 50)

    def test_cap_cannot_be_raised_past_the_ramp(self):
        mailbox_id = self._add()
        self._verify_ok(mailbox_id)  # anchors warmup at 'now' -> week 1
        self.assertEqual(mailboxes.set_daily_cap(self.ws, mailbox_id, 100), 5)
        # Lowering below the ramp is always allowed.
        self.assertEqual(mailboxes.set_daily_cap(self.ws, mailbox_id, 3), 3)
        # Past the ramp the customer's own cap rules.
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "UPDATE mailboxes SET warmup_started_at = ? "
                    "WHERE id = ? AND workspace_id = ?",
                    (db.utcnow_plus(days=-30), mailbox_id, self.ws))
        finally:
            con.close()
        self.assertEqual(mailboxes.set_daily_cap(self.ws, mailbox_id, 100), 100)

    def test_cap_route_is_workspace_scoped(self):
        mailbox_id = self._add()
        other_ws = lead_db.create_workspace("Other Co")
        self.assertIsNone(mailboxes.set_daily_cap(other_ws, mailbox_id, 3))

    # --- key rotation --------------------------------------------------------------

    def test_rotate_key_reencrypts_every_row(self):
        first = self._add("one@test-co.example")
        second = self._add("two@test-co.example")
        old_key, new_key = self.key, mailboxes.generate_key()
        with mock.patch.dict(os.environ, {"MAILBOX_ENC_KEY": new_key}):
            self.assertEqual(mailboxes.rotate_encryption(old_key, new_key), 2)
            for mailbox_id in (first, second):
                row = mailboxes.get_mailbox(self.ws, mailbox_id)
                self.assertEqual(
                    mailboxes.decrypt_creds(row["creds_encrypted"])["password"],
                    PASSWORD)
            # Second run: everything already on the new key.
            self.assertEqual(mailboxes.rotate_encryption(old_key, new_key), 0)

    # --- error marking ---------------------------------------------------------------

    def test_mark_error_flips_status(self):
        mailbox_id = self._add()
        self._verify_ok(mailbox_id)
        mailboxes.mark_error(self.ws, mailbox_id, "login refused")
        row = mailboxes.get_mailbox(self.ws, mailbox_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["last_error"], "login refused")


if __name__ == "__main__":
    unittest.main()
