"""Tenant mailboxes (V2 Stage 6, plans/v2/06): every workspace sends from
its OWN connected mailbox — Gmail/Workspace app password or generic
SMTP/IMAP. This module owns the mailbox rows, the credential encryption,
the connect-time verification, and the warmup ramp.

Credentials are encrypted at rest with Fernet; the key is MAILBOX_ENC_KEY in
.env. LOSING THE KEY BRICKS EVERY STORED CREDENTIAL — the key belongs in the
owner's password manager, nowhere else. Rotation (written now, per plan D,
not during an incident): put the NEW key in MAILBOX_ENC_KEY, move the old
one to MAILBOX_ENC_KEY_OLD, run `--rotate-key`, then delete the old key.

Verification on connect (plan A): SMTP login -> self-send with a nonce ->
IMAP read-back of that nonce. All three pass -> status 'active'. Any failure
-> status 'error' with a plain-words message the customer sees verbatim.

The warmup ramp (plan C1) is a product rule, not a suggestion: a mailbox may
send ~5/day in week 1, 10 in week 2, 20 in week 3, then whatever cap the
customer set. Customers can lower their cap, never raise it past the ramp.

Passwords never appear in logs, error messages, or query results handed to
templates — list_mailboxes() deliberately excludes the encrypted blob too.

Usage:
    python tools/mailboxes.py --genkey       # fresh MAILBOX_ENC_KEY for .env
    python tools/mailboxes.py --rotate-key   # re-encrypt all rows (see above)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.settings import require  # noqa: E402
import db  # noqa: E402
import lead_db  # noqa: E402

DEFAULT_CAP = 20
SMTP_TIMEOUT = 30
# IMAP read-back of the self-send: the message can take a few seconds to
# land, so poll a handful of times before calling it a failure.
VERIFY_RETRIES = 5
VERIFY_RETRY_SECONDS = 3

# Ramp thresholds (mailbox age in days -> max sends/day). After the last
# threshold the ramp stops binding and the customer's own cap rules.
RAMP = ((7, 5), (14, 10), (21, 20))

# Connect presets the UI offers. Gmail and Google Workspace share servers;
# app password required (no OAuth at launch, plans/v2/06 A).
PRESETS = {
    "gmail": {"smtp_host": "smtp.gmail.com", "smtp_port": 465,
              "imap_host": "imap.gmail.com", "imap_port": 993},
}


# --- Credential encryption -----------------------------------------------------


def generate_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(require("MAILBOX_ENC_KEY").encode())


def encrypt_creds(username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password})
    return _fernet().encrypt(payload.encode()).decode()


def decrypt_creds(token: str) -> dict:
    """{'username', 'password'} — decrypt at the moment of use, never log."""
    return json.loads(_fernet().decrypt(token.encode()).decode())


def rotate_encryption(old_key: str, new_key: str) -> int:
    """Re-encrypt every stored credential from old_key to new_key. Rows
    already readable with the new key are left alone, so re-running after an
    interrupted rotation is safe. A row neither key can read raises loudly —
    that means a third key was involved and silence would hide it. Returns
    how many rows were rewritten."""
    from cryptography.fernet import Fernet, InvalidToken
    f_new, f_old = Fernet(new_key.encode()), Fernet(old_key.encode())
    rewritten = 0
    con = lead_db.connect()
    try:
        rows = con.execute(
            "SELECT id, workspace_id, creds_encrypted FROM mailboxes"
        ).fetchall()
        with con.tx():
            for r in rows:
                token = r["creds_encrypted"].encode()
                try:
                    f_new.decrypt(token)
                    continue  # already on the new key
                except InvalidToken:
                    pass
                payload = f_old.decrypt(token)
                con.execute(
                    "UPDATE mailboxes SET creds_encrypted = ? "
                    "WHERE id = ? AND workspace_id = ?",
                    (f_new.encrypt(payload).decode(), r["id"],
                     r["workspace_id"]),
                )
                rewritten += 1
    finally:
        con.close()
    return rewritten


# --- CRUD (workspace-scoped, lead_db style) -------------------------------------

# Columns safe to hand to templates/logs — creds_encrypted stays out.
_PUBLIC_COLS = ("id, workspace_id, address, smtp_host, smtp_port, imap_host, "
                "imap_port, daily_cap, warmup_started_at, status, last_error, "
                "created_at")


SMTP_PORTS = (25, 465, 587, 2525)
IMAP_PORTS = (143, 993)


def _resolve(host: str) -> list[str]:
    """Every address `host` points at; empty when it doesn't resolve (the
    connection check then fails with its own plain message)."""
    import socket
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except OSError:
        return []


def check_server(host: str, port: int, allowed_ports: tuple[int, ...]) -> None:
    """Refuse a mail server the customer typed in that would make OUR server
    connect somewhere it shouldn't: a non-mail port, or an address inside our
    own network (localhost, private ranges, cloud metadata)."""
    import ipaddress

    host = (host or "").strip()
    if int(port) not in allowed_ports:
        ports = ", ".join(str(p) for p in allowed_ports)
        raise ValueError(f"Port {port} isn't a mail port. Use one of: {ports}.")
    try:
        addresses = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        addresses = _resolve(host)
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ValueError(
                f"{host} isn't a public mail server. Use the server name "
                "your email provider gives you.")


def add_mailbox(workspace_id: int, address: str, password: str, *,
                username: str | None = None,
                smtp_host: str, smtp_port: int,
                imap_host: str, imap_port: int) -> int:
    """Store a new mailbox (status 'unverified'). Returns its id."""
    address = (address or "").strip().lower()
    if not address or "@" not in address:
        raise ValueError("a real email address is required")
    if not (password or "").strip():
        raise ValueError("the app password is required")
    check_server(smtp_host, smtp_port, SMTP_PORTS)
    check_server(imap_host, imap_port, IMAP_PORTS)
    token = encrypt_creds(username or address, password)
    con = lead_db.connect()
    try:
        with con.tx():
            return con.insert_id(
                "INSERT INTO mailboxes (workspace_id, address, smtp_host, "
                "smtp_port, imap_host, imap_port, creds_encrypted, daily_cap, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "'unverified', ?)",
                (workspace_id, address, smtp_host, int(smtp_port), imap_host,
                 int(imap_port), token, DEFAULT_CAP, db.utcnow()),
            )
    finally:
        con.close()


def list_mailboxes(workspace_id: int) -> list[dict]:
    """One workspace's mailboxes WITHOUT the encrypted credentials."""
    con = lead_db.connect()
    try:
        rows = con.execute(
            f"SELECT {_PUBLIC_COLS} FROM mailboxes WHERE workspace_id = ? "
            "ORDER BY id",
            (workspace_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_mailbox(workspace_id: int, mailbox_id: int,
                con: db.DB | None = None) -> dict | None:
    """Full row including creds_encrypted — internal use only; never hand
    this dict to a template or a log line."""
    own = con is None
    con = con or lead_db.connect()
    try:
        row = con.execute(
            "SELECT * FROM mailboxes WHERE id = ? AND workspace_id = ?",
            (mailbox_id, workspace_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            con.close()


def active_mailboxes(con: db.DB, workspace_id: int) -> list[dict]:
    """Verified, sendable mailboxes for the sequencer (full rows)."""
    rows = con.execute(
        "SELECT * FROM mailboxes WHERE workspace_id = ? AND status = 'active' "
        "ORDER BY id",
        (workspace_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_mailbox(workspace_id: int, mailbox_id: int) -> None:
    """Remove the row — and with it the stored credentials."""
    con = lead_db.connect()
    try:
        with con.tx():
            con.execute(
                "DELETE FROM mailboxes WHERE id = ? AND workspace_id = ?",
                (mailbox_id, workspace_id),
            )
    finally:
        con.close()


def set_daily_cap(workspace_id: int, mailbox_id: int, cap: int) -> int | None:
    """Set a mailbox's cap, clamped to [1, ramp] — the customer can lower it,
    never raise it past the warmup ramp (plan C1). Returns the applied cap,
    or None if the mailbox doesn't exist in this workspace."""
    con = lead_db.connect()
    try:
        row = get_mailbox(workspace_id, mailbox_id, con)
        if row is None:
            return None
        ramp = ramp_cap(row["warmup_started_at"])
        applied = max(1, int(cap))
        if ramp is not None:
            applied = min(applied, ramp)
        with con.tx():
            con.execute(
                "UPDATE mailboxes SET daily_cap = ? "
                "WHERE id = ? AND workspace_id = ?",
                (applied, mailbox_id, workspace_id),
            )
        return applied
    finally:
        con.close()


def mark_error(workspace_id: int, mailbox_id: int, message: str,
               con: db.DB | None = None) -> None:
    """Flip a mailbox to 'error' (creds rotated, provider refused login, ...).
    The sequencer skips error mailboxes; the customer gets alerted by the
    caller. `message` must never contain a credential."""
    own = con is None
    con = con or lead_db.connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE mailboxes SET status = 'error', last_error = ? "
                "WHERE id = ? AND workspace_id = ?",
                ((message or "")[:300], mailbox_id, workspace_id),
            )
    finally:
        if own:
            con.close()


# --- Warmup ramp (plan C1) ------------------------------------------------------


def mailbox_age_days(warmup_started_at: str | None,
                     now: str | None = None) -> int:
    """Whole days since the mailbox first went active. Unverified (no
    anchor) counts as day 0 — brand new."""
    if not warmup_started_at:
        return 0
    now = now or db.utcnow()
    try:
        from datetime import datetime
        started = datetime.strptime(warmup_started_at, "%Y-%m-%d %H:%M:%S")
        current = datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return max(0, (current - started).days)


def ramp_cap(warmup_started_at: str | None, now: str | None = None) -> int | None:
    """Max sends/day the ramp allows at this mailbox age; None once the
    mailbox is past the ramp (the customer's own cap rules then)."""
    age = mailbox_age_days(warmup_started_at, now)
    for limit_days, cap in RAMP:
        if age < limit_days:
            return cap
    return None


def effective_cap(mailbox: dict, now: str | None = None) -> int:
    """What this mailbox may send today: min(customer cap, ramp)."""
    ramp = ramp_cap(mailbox.get("warmup_started_at"), now)
    cap = mailbox.get("daily_cap") or DEFAULT_CAP
    return min(cap, ramp) if ramp is not None else cap


# --- Connect-time verification (plan A) ------------------------------------------


def _friendly(step: str, exc: Exception) -> str:
    """Turn a transport exception into words a non-technical customer can
    act on. Never includes credentials."""
    import smtplib
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("The email provider rejected the login. For Gmail this "
                "almost always means the app password is wrong or 2-Step "
                "Verification is off. Make a fresh app password and try again.")
    text = str(exc)[:200]
    if step == "smtp":
        return (f"Couldn't reach the sending server: {text}. Check the SMTP "
                "server name and port.")
    if step == "imap":
        return (f"Sending works, but reading the inbox failed: {text}. Check "
                "the IMAP server name and port, and that IMAP is enabled in "
                "the mailbox settings.")
    return (f"The test email was sent but never showed up back in the inbox: "
            f"{text}")


def _default_smtp_send(host: str, port: int, username: str, password: str,
                       msg: EmailMessage) -> None:
    import smtplib
    with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg)


def _default_imap_subjects(host: str, port: int, username: str,
                           password: str) -> list[str]:
    from imap_tools import MailBox
    with MailBox(host, port).login(username, password) as mb:
        return [m.subject or "" for m in
                mb.fetch(limit=20, reverse=True, headers_only=True)]


def verify_mailbox(workspace_id: int, mailbox_id: int, *,
                   smtp_send=None, imap_subjects=None,
                   sleep=time.sleep) -> tuple[bool, str]:
    """The connect check: SMTP login + self-send, then IMAP read-back of the
    self-sent nonce. Updates the row's status and returns (ok, message).
    `smtp_send` / `imap_subjects` are injectable for offline tests."""
    smtp_send = smtp_send or _default_smtp_send
    imap_subjects = imap_subjects or _default_imap_subjects

    row = get_mailbox(workspace_id, mailbox_id)
    if row is None:
        return False, "Mailbox not found."
    creds = decrypt_creds(row["creds_encrypted"])
    nonce = uuid.uuid4().hex[:12]
    subject = f"Replyworthy connection check {nonce}"

    msg = EmailMessage()
    msg["From"] = row["address"]
    msg["To"] = row["address"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=row["address"].split("@")[-1])
    msg.set_content(
        "This is Replyworthy checking that your mailbox connection works.\n"
        "You can delete this email."
    )

    try:
        smtp_send(row["smtp_host"], row["smtp_port"] or 465,
                  creds["username"], creds["password"], msg)
    except Exception as exc:  # noqa: BLE001 - every transport error becomes words
        message = _friendly("smtp", exc)
        mark_error(workspace_id, mailbox_id, message)
        return False, message

    found = False
    last_exc: Exception | None = None
    for attempt in range(VERIFY_RETRIES):
        try:
            subjects = imap_subjects(row["imap_host"], row["imap_port"] or 993,
                                     creds["username"], creds["password"])
            last_exc = None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            subjects = []
        if any(nonce in s for s in subjects):
            found = True
            break
        if attempt < VERIFY_RETRIES - 1:
            sleep(VERIFY_RETRY_SECONDS)
    if not found:
        message = _friendly("imap" if last_exc else "readback",
                            last_exc or Exception("not found"))
        mark_error(workspace_id, mailbox_id, message)
        return False, message

    con = lead_db.connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE mailboxes SET status = 'active', last_error = NULL, "
                "warmup_started_at = COALESCE(warmup_started_at, ?) "
                "WHERE id = ? AND workspace_id = ?",
                (db.utcnow(), mailbox_id, workspace_id),
            )
    finally:
        con.close()
    return True, "Connected. This mailbox can now send."


def main() -> int:
    parser = argparse.ArgumentParser(description="Mailbox key utilities.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--genkey", action="store_true",
                       help="print a fresh MAILBOX_ENC_KEY for .env")
    group.add_argument("--rotate-key", action="store_true",
                       help="re-encrypt all rows from MAILBOX_ENC_KEY_OLD "
                            "to MAILBOX_ENC_KEY")
    args = parser.parse_args()

    if args.genkey:
        print(generate_key())
        return 0

    old_key = require("MAILBOX_ENC_KEY_OLD")
    new_key = require("MAILBOX_ENC_KEY")
    n = rotate_encryption(old_key, new_key)
    print(f"Re-encrypted {n} mailbox credential(s). "
          "Remove MAILBOX_ENC_KEY_OLD from .env now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
