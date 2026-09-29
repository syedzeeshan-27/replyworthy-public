"""The inbox poller must only touch mail that is plausibly ours.

A connected mailbox is frequently a real person's inbox, not a dedicated
outreach box. Before this suite the poller stored EVERY unread message in
INBOX/Spam as a needs_human "reply" (body and all) and marked it read in the
customer's mailbox. That is a privacy leak and it mutates mail we do not own.

The rule under test: we touch a message only when it matched a lead, or it is
a bounce that arrived after we connected, or it is unmatched but arrived after
we connected AND is shaped like a reply. Anything else is left completely
alone - not stored, and above all not marked read.

Run (from the repo root):
    python -m unittest tests.test_inbox_scope -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

try:
    from tests.tenancy import TenancyTestMixin
except ImportError:
    from tenancy import TenancyTestMixin

import db
import lead_db
import read_inbox

CONNECTED_AT = "2026-07-10 00:00:00"
BEFORE = "2026-07-01 12:00:00"   # sitting in the inbox before we connected
AFTER = "2026-07-12 12:00:00"    # arrived while we were polling

LEAD = {
    "name": "Window Dental", "city": "Boise ID", "place_id": "wd-1",
    "email_verified": "mx_ok", "score": 5,
    "email_subject": "quick question", "email_body": "hello body",
}


class FakeMessage:
    def __init__(self, uid, from_, subject, date, text="body",
                 in_reply_to="", references=""):
        self.uid = uid
        self.from_ = from_
        self.subject = subject
        self.date = date
        self.text = text
        self.html = ""
        self.headers = {}
        if in_reply_to:
            self.headers["in-reply-to"] = (in_reply_to,)
        if references:
            self.headers["references"] = (references,)


class FakeMailBox:
    """Stands in for imap_tools.MailBox. Records which uids got flagged."""

    def __init__(self, messages):
        self.messages = messages
        self.flagged: list[str] = []
        self.current = "INBOX"

    def __call__(self, host, port):
        return self

    def login(self, username, password):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def folder(self):
        outer = self

        class _Folder:
            def set(self, name):
                outer.current = name
                if name != "INBOX":
                    raise ValueError("no such folder")

        return _Folder()

    def fetch(self, criteria, mark_seen=False):
        return iter(self.messages) if self.current == "INBOX" else iter(())

    def flag(self, uid, flags, value):
        if value and "\\Seen" in flags:
            self.flagged.append(uid)


class InboxScopeTest(TenancyTestMixin, unittest.TestCase):

    def setUp(self):
        self.setup_tenancy()
        self.ws = lead_db.create_workspace("Test Co")

    def tearDown(self):
        self.teardown_tenancy()

    def _seed_lead_with_send(self, email: str) -> None:
        """A lead we actually emailed, so real replies have something to match."""
        lead_db.upsert_leads(self.ws, [dict(LEAD, email=email)])
        con = lead_db.connect()
        try:
            lead_id = con.execute(
                "SELECT id FROM leads WHERE workspace_id = ? AND lower(email) = ?",
                (self.ws, email)).fetchone()["id"]
            with con.tx():
                campaign_id = con.insert_id(
                    "INSERT INTO campaigns (workspace_id, name, daily_cap, "
                    "status) VALUES (?, 'C', 30, 'active')", (self.ws,))
                con.execute(
                    "INSERT INTO sends (workspace_id, lead_id, campaign_id, "
                    "step_number, scheduled_at, sent_at, status, message_id) "
                    "VALUES (?, ?, ?, 1, ?, ?, 'sent', ?)",
                    (self.ws, lead_id, campaign_id, db.utcnow(), db.utcnow(),
                     "<ours-123@leadgen>"))
        finally:
            con.close()

    def _store(self, uid, from_email, subject, received_at, **kw):
        con = lead_db.connect()
        try:
            with con.tx():
                return read_inbox.store_message(
                    con, "box@test-co.example", "INBOX", uid,
                    from_email=from_email, subject=subject, body="text",
                    received_at=received_at, scope_workspace_id=self.ws,
                    connected_at=CONNECTED_AT, **kw)
        finally:
            con.close()

    def _replies(self) -> list:
        con = lead_db.connect()
        try:
            return con.execute(
                "SELECT * FROM replies WHERE workspace_id = ?",
                (self.ws,)).fetchall()
        finally:
            con.close()

    # --- what must NOT be touched -------------------------------------------

    def test_newsletter_is_not_stored(self):
        """No thread headers, no Re:, matches no lead - not our business."""
        result = self._store("1", "growth@growth.twilio.com",
                             "Build better with Twilio", AFTER)
        self.assertEqual(result, read_inbox.SKIPPED)
        self.assertEqual(len(self._replies()), 0)

    def test_mail_predating_the_connection_is_not_stored(self):
        """Reply-shaped, but it was sitting there before we ever connected."""
        result = self._store("2", "friend@example.com", "Re: lunch tomorrow",
                             BEFORE, in_reply_to="<their-thread@example.com>")
        self.assertEqual(result, read_inbox.SKIPPED)
        self.assertEqual(len(self._replies()), 0)

    def test_old_bounce_for_unknown_recipient_is_not_stored(self):
        """A delivery failure from before we connected is somebody else's."""
        result = self._store("3", "mailer-daemon@googlemail.com",
                             "Delivery Status Notification (Failure)", BEFORE)
        self.assertEqual(result, read_inbox.SKIPPED)
        self.assertEqual(len(self._replies()), 0)

    # --- what must still be caught ------------------------------------------

    def test_reply_matching_a_lead_is_stored_even_if_old(self):
        """Matched by sender: it IS ours, whatever the clock says."""
        self._seed_lead_with_send("owner@window-dental.example")
        result = self._store("4", "owner@window-dental.example",
                             "Re: quick question", BEFORE)
        self.assertEqual(result, read_inbox.STORED)
        rows = self._replies()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["lead_id"])

    def test_unmatched_reply_after_connecting_is_still_stored(self):
        """Path (d) proper: a message that matches NO lead by any route —
        unknown sender (not b), thread id we never sent (not a), subject that
        is not one of our sent subjects (not c) — but is reply-shaped and
        arrived after connect, so it is stored as needs_human for a human. The
        sender/subject deliberately avoid the seeded lead so paths a/b/c cannot
        rescue it; otherwise this would not exercise (d) at all."""
        self._seed_lead_with_send("owner@window-dental.example")
        result = self._store("5", "stranger@nowhere.example",
                             "Re: your note", AFTER,
                             in_reply_to="<unrelated-thread@nowhere.example>")
        self.assertEqual(result, read_inbox.STORED)
        rows = self._replies()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["lead_id"],
                          "must be unmatched (path d), not matched via a/b/c")
        self.assertEqual(rows[0]["classification"], "needs_human")

    # --- the destructive part ------------------------------------------------

    def test_poller_never_marks_skipped_mail_seen(self):
        """The one that burned us: unread personal mail must stay unread."""
        box = FakeMailBox([
            FakeMessage("101", "growth@growth.twilio.com", "Build better",
                        datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)),
            FakeMessage("102", "no-reply@kaggle.com", "Your weekly digest",
                        datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)),
        ])
        with mock.patch("imap_tools.MailBox", box):
            new = read_inbox._poll_imap(
                "box@test-co.example", "imap.test", 993, "u", "p",
                ("INBOX",), self.ws, connected_at=CONNECTED_AT)

        self.assertEqual(new, 0)
        self.assertEqual(box.flagged, [],
                         "skipped mail must never be marked read")
        self.assertEqual(len(self._replies()), 0)


if __name__ == "__main__":
    unittest.main()
