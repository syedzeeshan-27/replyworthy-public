"""SMTP sender for outreach emails — dry-run by default.

Stage-2 sending (plans/02_STAGE2_OUTREACH_ENGINE.md C1) plus the V2 Stage 6
tenant path. Two transports, same hygiene (our own Message-ID for threading,
List-Unsubscribe headers, CAN-SPAM footer):
  - send_email():        an ENV-configured mailbox (the owner's V1 setup,
                         also system/transactional mail).
  - send_from_mailbox(): a workspace's own connected mailbox row
                         (plans/v2/06 B) — creds decrypted at use.

SAFETY: `DRY_RUN` defaults to TRUE and is the platform MASTER override.
Until the .env explicitly sets `DRY_RUN=false`, nothing leaves the machine —
sends are logged and a fake message id is returned so the whole pipeline can
rehearse. On top of it each workspace has its own go-live switch
(workspace_live) gated by the onboarding checklist; trial plans are
practice-only always.

Env (.env):
    SMTP_MAILBOXES      comma-separated sending addresses
                        e.g. zee@trydomain.com,outreach@trydomain.com
    SMTP_PASS_<LOCAL>   app password per mailbox; <LOCAL> is the local part
                        uppercased, non-alphanumerics -> "_"
                        (zee@... -> SMTP_PASS_ZEE)
    SENDER_ADDRESS      physical mailing address (CAN-SPAM; required for real sends)
    BASE_URL            web app root for unsubscribe links (localhost until deploy)
    UNSUBSCRIBE_SECRET  HMAC key for unsubscribe tokens
    DRY_RUN             "false" to actually send; anything else = dry run

Usage (single test send):
    python tools/send_email.py --to you@example.com --subject hi --body "test"
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import smtplib
import sys
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import require  # noqa: E402

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT = 30


def dry_run_enabled() -> bool:
    """True unless .env explicitly says DRY_RUN=false. Default-safe."""
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"


def workspace_live(ws: dict | None) -> bool:
    """May THIS workspace's outreach leave the machine for real (V2 Stage 6,
    plans/v2/06 B)? The global DRY_RUN env stays the platform master override
    (true = nobody sends live, full stop); on top of it the workspace must
    have flipped its own go-live switch. Trial workspaces are practice-only
    whatever their flags say (plans/v2/05 A)."""
    if ws is None or dry_run_enabled():
        return False
    if ws.get("plan") == "trial":
        return False
    return bool(ws.get("live_sending"))


def mailboxes() -> list[str]:
    raw = os.environ.get("SMTP_MAILBOXES", "")
    return [m.strip().lower() for m in raw.split(",") if m.strip()]


def _pass_env_key(mailbox: str) -> str:
    local = mailbox.split("@")[0]
    return "SMTP_PASS_" + re.sub(r"[^A-Za-z0-9]", "_", local).upper()


def mailbox_password(mailbox: str) -> str:
    return require(_pass_env_key(mailbox))


# --- Unsubscribe tokens (HMAC-signed workspace+lead id; verified by the web
# endpoint). The workspace is baked into the signature so a token minted for
# workspace A can never suppress anything in workspace B (plans/v2/04 D) —
# except the global hard-suppression the endpoint applies, which is global on
# purpose.

def _unsub_sig(workspace_id: int, lead_id: int) -> str:
    key = require("UNSUBSCRIBE_SECRET").encode()
    payload = f"{workspace_id}:{lead_id}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:24]


def make_unsub_token(workspace_id: int, lead_id: int) -> str:
    raw = f"{workspace_id}:{lead_id}:{_unsub_sig(workspace_id, lead_id)}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_unsub_token(token: str) -> tuple[int, int] | None:
    """(workspace_id, lead_id) if the token is valid, else None."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ws_str, lead_id_str, sig = raw.split(":", 2)
        workspace_id, lead_id = int(ws_str), int(lead_id_str)
    except (ValueError, UnicodeDecodeError):
        return None
    # Compared as bytes: compare_digest raises on a non-ASCII str, which a
    # crafted token would turn into a 500.
    if hmac.compare_digest(sig.encode(),
                           _unsub_sig(workspace_id, lead_id).encode()):
        return workspace_id, lead_id
    return None


def unsub_url(workspace_id: int, lead_id: int) -> str:
    base = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/unsubscribe/{make_unsub_token(workspace_id, lead_id)}"


def _footer(workspace_id: int | None, lead_id: int | None,
            name: str | None = None, address: str | None = None,
            live: bool | None = None) -> str:
    """CAN-SPAM block: who we are, where we are, how to opt out.
    name/address default to the platform's env identity (the V1 owner path);
    tenant sends pass the workspace's own identity in (plans/v2/06 B)."""
    if name is None:
        name = os.environ.get("SENDER_NAME", "")
    if address is None:
        address = os.environ.get("SENDER_ADDRESS", "")
    if live is None:
        live = not dry_run_enabled()
    if not address and live:
        # A real cold send without a physical address is a legal problem.
        raise RuntimeError(
            "No physical mailing address is set - required on real sends (CAN-SPAM)."
        )
    lines = ["", "--", f"{name} · {address}".strip(" ·")]
    optout = 'Don\'t want these? Reply "no thanks" and you won\'t hear from me again.'
    if lead_id is not None:
        optout += f"\nOr one click: {unsub_url(workspace_id, lead_id)}"
    lines.append(optout)
    return "\n".join(lines)


def _compose(from_addr: str, to_addr: str, subject: str, body: str,
             lead_id: int | None, workspace_id: int | None,
             in_reply_to: str | None, footer: str) -> tuple[EmailMessage, str]:
    """One outreach message with the cold-email hygiene headers. Returns
    (message, message_id)."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=from_addr.split("@")[-1])
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    unsub_parts = [f"<mailto:{from_addr}?subject=unsubscribe>"]
    if lead_id is not None:
        unsub_parts.append(f"<{unsub_url(workspace_id, lead_id)}>")
    msg["List-Unsubscribe"] = ", ".join(unsub_parts)
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body + footer)
    return msg, message_id


def send_email(
    mailbox: str,
    to_addr: str,
    subject: str,
    body: str,
    lead_id: int | None = None,
    workspace_id: int | None = None,
    in_reply_to: str | None = None,
) -> str:
    """Send one email from an ENV-configured mailbox (the V1 owner path) —
    or log it in dry-run mode. Returns the Message-ID."""
    if lead_id is not None and workspace_id is None:
        # An unsubscribe token without a workspace can't exist (plans/v2/04 D).
        raise ValueError("send_email needs workspace_id whenever lead_id is set")
    msg, message_id = _compose(mailbox, to_addr, subject, body, lead_id,
                               workspace_id, in_reply_to,
                               _footer(workspace_id, lead_id))

    if dry_run_enabled():
        print(
            f"  [DRY RUN] would send from={mailbox} to={to_addr} "
            f"subject={subject!r} thread={'yes' if in_reply_to else 'new'}"
        )
        return f"<dryrun-{uuid.uuid4().hex}@{mailbox.split('@')[-1] or 'local'}>"

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(mailbox, mailbox_password(mailbox))
        smtp.send_message(msg)
    print(f"  sent from={mailbox} to={to_addr} subject={subject!r}")
    return message_id


def send_from_mailbox(
    mailbox_row: dict,
    creds: dict,
    to_addr: str,
    subject: str,
    body: str,
    *,
    lead_id: int | None = None,
    workspace_id: int | None = None,
    in_reply_to: str | None = None,
    footer_name: str = "",
    postal_address: str = "",
    live: bool = False,
) -> str:
    """Send one outreach email through a TENANT's own connected mailbox
    (V2 Stage 6, plans/v2/06 B). `creds` is the decrypted dict from
    mailboxes.decrypt_creds — decrypted at the moment of use and never
    logged. live=False is practice mode: printed, fake message id, nothing
    leaves the machine. Returns the Message-ID."""
    if lead_id is not None and workspace_id is None:
        raise ValueError("send_from_mailbox needs workspace_id whenever lead_id is set")
    from_addr = mailbox_row["address"]
    msg, message_id = _compose(
        from_addr, to_addr, subject, body, lead_id, workspace_id, in_reply_to,
        _footer(workspace_id, lead_id, name=footer_name,
                address=postal_address, live=live),
    )

    if not live:
        print(
            f"  [PRACTICE] would send from={from_addr} to={to_addr} "
            f"subject={subject!r} thread={'yes' if in_reply_to else 'new'}"
        )
        return f"<practice-{uuid.uuid4().hex}@{from_addr.split('@')[-1] or 'local'}>"

    with smtplib.SMTP_SSL(mailbox_row["smtp_host"],
                          mailbox_row["smtp_port"] or SMTP_PORT,
                          timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(creds["username"], creds["password"])
        smtp.send_message(msg)
    print(f"  sent from={from_addr} to={to_addr} subject={subject!r}")
    return message_id


def _compose_plain(from_addr: str, to_addr: str, subject: str, body: str,
                   in_reply_to: str | None) -> tuple[EmailMessage, str]:
    """A plain personal email: threading headers only, NO cold-email hygiene
    (no List-Unsubscribe) and NO footer. For a human-written 1:1 reply to a
    prospect who already answered — it must read like a person, not a blast.
    Returns (message, message_id)."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    message_id = make_msgid(domain=from_addr.split("@")[-1])
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg, message_id


def send_reply_from_mailbox(mailbox_row: dict, creds: dict, to_addr: str,
                            subject: str, body: str, *,
                            in_reply_to: str | None = None,
                            live: bool = False) -> str:
    """Send a human-written 1:1 reply through a TENANT's own mailbox — no
    footer, no unsubscribe headers (the prospect started this thread).
    live=False is practice mode: printed, fake id, nothing leaves the machine.
    Returns the Message-ID."""
    from_addr = mailbox_row["address"]
    msg, message_id = _compose_plain(from_addr, to_addr, subject, body,
                                     in_reply_to)
    if not live:
        print(f"  [PRACTICE] would reply from={from_addr} to={to_addr} "
              f"subject={subject!r}")
        return f"<practice-{uuid.uuid4().hex}@{from_addr.split('@')[-1] or 'local'}>"
    with smtplib.SMTP_SSL(mailbox_row["smtp_host"],
                          mailbox_row["smtp_port"] or SMTP_PORT,
                          timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(creds["username"], creds["password"])
        smtp.send_message(msg)
    print(f"  sent reply from={from_addr} to={to_addr} subject={subject!r}")
    return message_id


def send_reply_env(mailbox: str, to_addr: str, subject: str, body: str, *,
                   in_reply_to: str | None = None, live: bool = False) -> str:
    """Owner V1 path: a human-written 1:1 reply from an ENV-configured mailbox —
    no footer, no unsubscribe headers. Returns the Message-ID."""
    msg, message_id = _compose_plain(mailbox, to_addr, subject, body,
                                     in_reply_to)
    if not live:
        print(f"  [PRACTICE] would reply from={mailbox} to={to_addr} "
              f"subject={subject!r}")
        return f"<practice-{uuid.uuid4().hex}@{mailbox.split('@')[-1] or 'local'}>"
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(mailbox, mailbox_password(mailbox))
        smtp.send_message(msg)
    print(f"  sent reply from={mailbox} to={to_addr} subject={subject!r}")
    return message_id


def send_system_email(to_addr: str, subject: str, body: str) -> bool:
    """Transactional mail (password resets) — system-mail mode, not campaign
    mode (plans/v2/04 A): no CAN-SPAM footer, no unsubscribe headers, no lead.
    DRY_RUN prints the full body so reset links are usable in local dev.
    Returns True if handed to SMTP (or printed in dry-run)."""
    if dry_run_enabled():
        print(f"  [DRY RUN] system mail to={to_addr} subject={subject!r}\n{body}")
        return True
    boxes = mailboxes()
    if not boxes:
        print("  system mail failed: SMTP_MAILBOXES is empty in .env", file=sys.stderr)
        return False
    mailbox = boxes[0]
    msg = EmailMessage()
    msg["From"] = mailbox
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=mailbox.split("@")[-1])
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(mailbox, mailbox_password(mailbox))
        smtp.send_message(msg)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one email (DRY_RUN-aware).")
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--mailbox", help="defaults to the first configured mailbox")
    args = parser.parse_args()

    boxes = mailboxes()
    mailbox = args.mailbox or (boxes[0] if boxes else "dryrun@localhost")
    mid = send_email(mailbox, args.to, args.subject, args.body)
    print(f"message-id: {mid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
