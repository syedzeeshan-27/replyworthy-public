"""Read replies (and bounces) from the sending mailboxes into the replies table.

Stage-3 inbox reader (plans/03_STAGE3_REPLY_HANDLING.md A). Each run polls
every configured mailbox over IMAP (same creds as SMTP sending), looking at
UNSEEN messages in INBOX *and* Spam — real replies land in spam sometimes,
and seeing our own campaigns spam-foldered early is a warmup warning.

Matching a message to a lead, most to least reliable:
  a. In-Reply-To/References contains a Message-ID we generated -> exact send.
  b. Sender address equals a lead's email -> that lead's latest sent send.
  c. Subject fuzzy-matches "Re: <a step-1 subject>" of a sent send.
  d. Nothing, but it is shaped like a reply and arrived after we connected
     -> stored unmatched, pre-classified needs_human (a human looks).

Scope — we only touch mail that is plausibly ours. A connected mailbox is
very often a real person's inbox, not a dedicated outreach box, so anything
that fails all of (a)-(d) is left ALONE: not stored, and never flagged Seen.
Reading someone's newsletters into the replies table and marking their unread
mail read is not ours to do. The two gates on path (d) are `connected_at`
(mail already sitting there when the mailbox was connected can never be a
reply to a send we had not made yet) and reply-shape (a real reply carries
In-Reply-To/References or a "Re:" subject; bulk mail carries neither).

Bounces (mailer-daemon / delivery-status reports) are classified 'bounce'
mechanically — no Claude tokens on machine-generated mail — but they too must
post-date the connection to count as ours.

Idempotency: replies carry a UNIQUE (mailbox, "folder:uid") key; refetching
is a no-op, and messages are only flagged Seen once stored.

Usage:
    python tools/read_inbox.py --poll
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lead_db  # noqa: E402
import mailboxes  # noqa: E402 - the Stage-6 tenant-mailbox module
import notify  # noqa: E402
from send_email import mailbox_password  # noqa: E402
from send_email import mailboxes as env_mailboxes  # noqa: E402

IMAP_HOST = "imap.gmail.com"
SPAM_FOLDER = "[Gmail]/Spam"
# Folders polled on a tenant mailbox: Gmail's spam name plus the common
# generic one; folders that don't exist on a host are skipped.
TENANT_FOLDERS = ("INBOX", SPAM_FOLDER, "Junk")

# store_message outcomes. SKIPPED means "not ours" — the caller must leave
# the message untouched, which above all means NOT marking it Seen.
STORED = "stored"
DUPLICATE = "duplicate"
SKIPPED = "skipped"

REPLY_SUBJECT_PREFIXES = ("re:", "fwd:", "fw:")

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
# Lines that start the quoted history in common mail clients.
QUOTE_MARKERS = (
    re.compile(r"^On .{0,200} wrote:\s*$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^From:\s.+@.+", re.IGNORECASE),
    re.compile(r"^_{10,}\s*$"),
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
FAILED_RCPT_RE = re.compile(
    r"(?:Final-Recipient:.*?;\s*|X-Failed-Recipients:\s*)([^\s;<>]+@[^\s;<>]+)",
    re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """Text above the first quoted-history marker; '>' quote lines dropped."""
    lines = []
    for line in (text or "").splitlines():
        if any(marker.match(line.strip()) for marker in QUOTE_MARKERS):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def is_bounce(from_email: str, subject: str, body: str) -> bool:
    subj_l = (subject or "").lower()
    if "(delay)" in subj_l or "delayed" in subj_l:
        return False  # "still trying" notice: the address may yet work
    from_l = (from_email or "").lower()
    if any(from_l.startswith(s) or s in from_l for s in BOUNCE_SENDERS):
        return True
    return "delivery status notification" in subj_l or "undeliverable" in subj_l


def bounced_recipient(body: str) -> str | None:
    """The failed address out of a DSN body, best-effort."""
    m = FAILED_RCPT_RE.search(body or "")
    if m:
        return m.group(1).strip().strip("<>").lower()
    m = EMAIL_RE.search(body or "")
    return m.group(0).lower() if m else None


def looks_like_reply(subject: str | None, in_reply_to: str | None,
                     references: str | None) -> bool:
    """True if the message is shaped like a reply to SOMETHING. Mail clients
    set In-Reply-To/References when a human hits reply; bulk senders don't."""
    if (in_reply_to or "").strip() or (references or "").strip():
        return True
    return (subject or "").strip().lower().startswith(REPLY_SUBJECT_PREFIXES)


def arrived_after_connecting(received_at: str | None,
                             connected_at: str | None) -> bool:
    """Mail that predates the mailbox connection cannot be a reply to us.
    `connected_at` None = the owner's V1 env mailboxes, which have no connect
    date and are dedicated outreach boxes: no date gate for them."""
    if not connected_at:
        return True
    return (received_at or "") >= connected_at


def _norm_subject(subject: str | None) -> str:
    s = (subject or "").strip().lower()
    while s.startswith(("re:", "fwd:", "fw:")):
        s = s.split(":", 1)[1].strip()
    return s


# NOTE on workspace scoping (plans/v2/04 B): the queries in match_lead and
# the bounce path below are the ROUTER — a message arrives at a physical
# mailbox and these lookups decide WHICH workspace it belongs to, so they
# must span workspaces by design. They are the explicitly whitelisted
# exception (sql-audit: allow-cross-workspace); everything downstream uses
# the workspace_id they resolve. Since v2/06 a TENANT mailbox belongs to one
# workspace, so its polls pass scope_workspace_id and every candidate from
# another workspace is discarded — a reply arriving at workspace A's own
# mailbox can never touch workspace B. The owner's env mailboxes (the V1
# shared boxes) still route unscoped.


def match_lead(con, from_email: str, in_reply_to: str, references: str,
               subject: str, scope_workspace_id: int | None = None,
               ) -> tuple[int | None, int | None, int | None]:
    """Returns (workspace_id, lead_id, send_id) or (None, None, None)."""

    def in_scope(workspace_id: int) -> bool:
        return scope_workspace_id is None or workspace_id == scope_workspace_id

    # a. thread headers carry one of our Message-IDs
    header_blob = f"{in_reply_to or ''} {references or ''}"
    ids = re.findall(r"<[^<>]+>", header_blob)
    for mid in ids:
        rows = con.execute(
            "SELECT id, lead_id, workspace_id FROM sends "
            "WHERE message_id = ? AND status = 'sent'",
            (mid,),
        ).fetchall()
        for row in rows:
            if in_scope(row["workspace_id"]):
                return row["workspace_id"], row["lead_id"], row["id"]

    # b. sender address is a known lead; if several workspaces know this
    # address, the one that actually emailed them (latest sent send) wins.
    candidates = con.execute(
        "SELECT id, workspace_id FROM leads WHERE lower(email) = ? ORDER BY id",
        ((from_email or "").lower(),),
    ).fetchall()
    candidates = [c for c in candidates if in_scope(c["workspace_id"])]
    if candidates:
        best = None  # (sent_at, workspace_id, lead_id, send_id)
        for cand in candidates:
            send = con.execute(
                "SELECT id, sent_at FROM sends WHERE workspace_id = ? "
                "AND lead_id = ? AND status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 1",
                (cand["workspace_id"], cand["id"]),
            ).fetchone()
            if send and (best is None or (send["sent_at"] or "") > best[0]):
                best = (send["sent_at"] or "", cand["workspace_id"],
                        cand["id"], send["id"])
        if best:
            return best[1], best[2], best[3]
        first = candidates[0]
        return first["workspace_id"], first["id"], None

    # c. subject matches a step-1 subject we sent
    norm = _norm_subject(subject)
    if norm:
        rows = con.execute(
            "SELECT s.id AS send_id, s.lead_id, s.workspace_id FROM sends s "
            "JOIN leads l ON l.id = s.lead_id "
            "WHERE s.status = 'sent' AND lower(l.email_subject) = ? "
            "ORDER BY s.sent_at DESC LIMIT 10",
            (norm,),
        ).fetchall()
        for row in rows:
            if in_scope(row["workspace_id"]):
                return row["workspace_id"], row["lead_id"], row["send_id"]

    return None, None, None


def store_message(con, mailbox: str, folder: str, uid: str, from_email: str,
                  subject: str, body: str, received_at: str,
                  in_reply_to: str = "", references: str = "",
                  scope_workspace_id: int | None = None,
                  connected_at: str | None = None) -> str:
    """Match + insert one message. Returns STORED (newly inserted), DUPLICATE
    (already had it — idempotent on (mailbox, folder:uid)), or SKIPPED (not
    ours: the caller must not touch the message at all).
    The reply lands in the matched lead's workspace; unmatched mail goes to
    the mailbox owner's workspace (v2/06: a tenant mailbox passes its
    scope_workspace_id) — the owner workspace for the V1 env mailboxes."""
    from_email = (from_email or "").lower()
    workspace_id = None

    if is_bounce(from_email, subject, body):
        # Machine mail: classify mechanically, never spend Claude tokens.
        if not arrived_after_connecting(received_at, connected_at):
            return SKIPPED  # a delivery failure predating us is someone else's
        rcpt = bounced_recipient(body)
        lead_id = send_id = None
        if rcpt:
            # Router query (sql-audit: allow-cross-workspace) — resolves which
            # workspace's lead bounced; newest lead wins if several match.
            rows = con.execute(
                "SELECT id, workspace_id FROM leads WHERE lower(email) = ? "
                "ORDER BY id DESC",
                (rcpt,),
            ).fetchall()
            for row in rows:
                if (scope_workspace_id is None
                        or row["workspace_id"] == scope_workspace_id):
                    lead_id, workspace_id = row["id"], row["workspace_id"]
                    break
        classification, confidence = "bounce", "high"
        summary = f"delivery failure for {rcpt or 'unknown recipient'}"
        action = "none - handled automatically"
    else:
        workspace_id, lead_id, send_id = match_lead(
            con, from_email, in_reply_to, references, subject,
            scope_workspace_id,
        )
        classification = confidence = summary = action = None
        if lead_id is None:
            # Path (d). Unmatched mail is only ours if it could plausibly be a
            # reply at all — otherwise it is the mailbox owner's own post and
            # we leave it exactly as we found it.
            if not (arrived_after_connecting(received_at, connected_at)
                    and looks_like_reply(subject, in_reply_to, references)):
                return SKIPPED
            classification, confidence = "needs_human", "low"
            summary = f"unmatched sender {from_email}"
            action = "review manually - could not link to a lead"

    if workspace_id is None:
        workspace_id = scope_workspace_id or lead_db.OWNER_WORKSPACE_ID

    cur = con.execute(
        "INSERT INTO replies "
        "(workspace_id, lead_id, send_id, mailbox, from_email, subject, "
        " body_text, received_at, imap_uid, classification, confidence, "
        " summary, suggested_action) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        (workspace_id, lead_id, send_id, mailbox, from_email, subject,
         strip_quoted(body), received_at, f"{folder}:{uid}", classification,
         confidence, summary, action),
    )
    return STORED if cur.rowcount > 0 else DUPLICATE


def _poll_imap(address: str, imap_host: str, imap_port: int | None,
               username: str, password: str, folders: tuple[str, ...],
               scope_workspace_id: int | None,
               connected_at: str | None = None) -> int:
    """Fetch UNSEEN from the given folders of one IMAP account and store the
    messages that are ours (scoped to the mailbox owner's workspace when
    given). Mail that isn't ours is left untouched and stays unread.
    Returns the new-reply count. The password is used for login only —
    never printed, never stored."""
    from imap_tools import AND, MailBox  # lazy: only needed on live polls

    new = 0
    con = lead_db.connect()
    try:
        with MailBox(imap_host, imap_port or 993).login(username, password) as mb:
            for folder in folders:
                try:
                    mb.folder.set(folder)
                except Exception:  # folder may not exist on this host
                    continue
                for msg in mb.fetch(AND(seen=False), mark_seen=False):
                    received = (msg.date or datetime.now(timezone.utc)).astimezone(timezone.utc)
                    headers = msg.headers or {}
                    with con.tx():
                        result = store_message(
                            con, address, folder, msg.uid,
                            from_email=msg.from_ or "",
                            subject=msg.subject or "",
                            body=msg.text or msg.html or "",
                            received_at=received.strftime("%Y-%m-%d %H:%M:%S"),
                            in_reply_to=(headers.get("in-reply-to", ("",)) or ("",))[0],
                            references=(headers.get("references", ("",)) or ("",))[0],
                            scope_workspace_id=scope_workspace_id,
                            connected_at=connected_at,
                        )
                    if result == SKIPPED:
                        continue  # not ours — leave it unread, leave it alone
                    mb.flag(msg.uid, ["\\Seen"], True)
                    if result == STORED:
                        new += 1
                        print(f"  [{address}/{folder}] stored reply from {msg.from_}")
    finally:
        con.close()
    return new


def poll_mailbox(mailbox: str) -> int:
    """V1 owner path: one ENV-configured Gmail mailbox, unscoped routing."""
    return _poll_imap(mailbox, IMAP_HOST, 993, mailbox,
                      mailbox_password(mailbox), ("INBOX", SPAM_FOLDER), None)


def poll_workspace_mailbox(row: dict) -> int:
    """One tenant mailbox (v2/06): decrypt at use, poll its own IMAP server,
    scope every match to its workspace. A login/connection failure flips the
    mailbox to 'error' and alerts the CUSTOMER (plans/v2/06 D)."""
    try:
        creds = mailboxes.decrypt_creds(row["creds_encrypted"])
        return _poll_imap(row["address"], row["imap_host"], row["imap_port"],
                          creds["username"], creds["password"],
                          TENANT_FOLDERS, row["workspace_id"],
                          connected_at=row["created_at"])
    except Exception as exc:  # noqa: BLE001 - one bad mailbox must not kill the poll
        mailboxes.mark_error(row["workspace_id"], row["id"], str(exc)[:200])
        notify.send_alert(
            row["workspace_id"],
            f"Mailbox {row['address']} stopped working",
            "Reading its inbox failed (usually a changed password). Replies "
            "and sending are paused for this mailbox until you reconnect it "
            "on the Mailboxes page.",
            severity="urgent", kind=f"mailbox_error_{row['id']}",
        )
        print(f"  [{row['address']}] IMAP poll failed: {exc}", file=sys.stderr)
        return 0


def poll_all() -> int:
    """Every workspace's own mailboxes first (v2/06), then the owner's V1
    env mailboxes. One broken mailbox never stops the others."""
    total = 0
    con = lead_db.connect()
    try:
        tenant_rows = []
        for workspace_id in lead_db.workspace_ids(con):
            tenant_rows.extend(mailboxes.active_mailboxes(con, workspace_id))
    finally:
        con.close()
    for row in tenant_rows:
        total += poll_workspace_mailbox(row)

    env_boxes = env_mailboxes()
    if not env_boxes and not tenant_rows:
        print("No mailboxes connected or configured - nothing to poll.")
        return 0
    for mailbox in env_boxes:
        try:
            total += poll_mailbox(mailbox)
        except Exception as exc:  # noqa: BLE001 - one bad mailbox must not kill the poll
            print(f"  [{mailbox}] IMAP poll failed: {exc}", file=sys.stderr)
    print(f"Inbox poll done: {total} new message(s).")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll sending mailboxes for replies.")
    parser.add_argument("--poll", action="store_true", required=True)
    args = parser.parse_args()
    poll_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
