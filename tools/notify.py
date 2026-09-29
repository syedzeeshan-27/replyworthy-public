"""Phone alerts — Telegram first, self-email fallback, stderr as last resort.

Stage-5 alerting (plans/05_STAGE5_ALERTS_DASHBOARD.md A/B). The product
promise is "ping me ONLY when a lead needs human attention", so this module
enforces the noise rules itself:

  - Dedupe: one alert per (lead, kind) per 24h — a lead classified
    `interested` twice in a day buzzes the phone once. Tracked in the
    `alerts` table, which doubles as the alert history.
  - Batching: callers collect a tick's alerts and hand them over in ONE
    send_batch() call -> one combined Telegram message, not a burst.
  - Channel fallback: Telegram env missing/down -> email to ALERT_EMAIL via
    the Stage-2 SMTP creds -> stderr print. Alerting never raises: a dead
    alert channel must not stop replies from being processed.

Messages are plain text on purpose — lead names full of `_`/`*` break
Telegram's markdown parser.

Env (.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_EMAIL (fallback).

Usage (send a test alert):
    python tools/notify.py --test
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import lead_db  # noqa: E402

DEDUPE_HOURS = 24
TELEGRAM_TIMEOUT = 15
SEVERITY_TAG = {"urgent": "[URGENT]", "warn": "[WARN]", "info": "[INFO]"}


def _base_url() -> str:
    return (os.environ.get("BASE_URL") or "http://localhost:8000").rstrip("/")


def _telegram_send(text: str) -> bool:
    """True on delivered. Tests replace this function."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=TELEGRAM_TIMEOUT,
        )
        return resp.ok
    except Exception as exc:  # noqa: BLE001 - fall through to the next channel
        print(f"  [notify] telegram failed: {exc}", file=sys.stderr)
        return False


def _email_send(subject: str, text: str) -> bool:
    """Self-email fallback over the Stage-2 SMTP creds. Tests replace this."""
    to_addr = os.environ.get("ALERT_EMAIL", "").strip()
    if not to_addr:
        return False
    try:
        import smtplib
        from email.message import EmailMessage

        from send_email import SMTP_HOST, SMTP_PORT, SMTP_TIMEOUT, mailbox_password, mailboxes

        boxes = mailboxes()
        if not boxes:
            return False
        msg = EmailMessage()
        msg["From"] = boxes[0]
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(text)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
            smtp.login(boxes[0], mailbox_password(boxes[0]))
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - fall through to stderr
        print(f"  [notify] email fallback failed: {exc}", file=sys.stderr)
        return False


def _workspace_email_send(con, workspace_id: int, subject: str,
                          text: str) -> bool:
    """v2/06 D: a tenant workspace's alerts also go to ITS OWN alert email —
    the customer must hear their mailbox broke, not just the owner. The
    owner's phone chain keeps covering everything; this never raises."""
    if workspace_id == lead_db.OWNER_WORKSPACE_ID:
        return False  # the owner already gets the Telegram/ALERT_EMAIL chain
    try:
        row = con.execute(
            "SELECT alert_email FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        to_addr = ((row["alert_email"] if row else "") or "").strip()
        if not to_addr:
            return False
        from send_email import send_system_email
        return send_system_email(to_addr, subject, text)
    except Exception as exc:  # noqa: BLE001 - alerting must never break the pipeline
        print(f"  [notify] workspace alert email failed: {exc}", file=sys.stderr)
        return False


def _format(item: dict) -> str:
    tag = SEVERITY_TAG.get(item.get("severity", "info"), "[INFO]")
    lines = [f"{tag} {item['title']}"]
    if item.get("body"):
        lines.append(str(item["body"]).strip())
    if item.get("lead_id"):
        lines.append(f"{_base_url()}/lead/{item['lead_id']}")
    return "\n".join(lines)


def _is_dupe(con, workspace_id: int, item: dict) -> bool:
    cutoff = db.utcnow_plus(hours=-DEDUPE_HOURS)
    if item.get("lead_id"):
        row = con.execute(
            "SELECT 1 FROM alerts WHERE workspace_id = ? AND lead_id = ? "
            "AND kind = ? AND created_at > ? LIMIT 1",
            (workspace_id, item["lead_id"], item["kind"], cutoff),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT 1 FROM alerts WHERE workspace_id = ? AND lead_id IS NULL "
            "AND kind = ? AND created_at > ? LIMIT 1",
            (workspace_id, item["kind"], cutoff),
        ).fetchone()
    return row is not None


def send_batch(workspace_id: int, items: list[dict]) -> dict:
    """Dedupe, combine into one message, deliver, record. Never raises.
    One batch belongs to ONE workspace (its rows feed that workspace's
    dashboard); the phone channel stays the owner's. Item keys: title
    (required), body, lead_id, kind, severity."""
    counters = {"sent": 0, "deduped": 0, "channel": None}
    if not items:
        return counters
    try:
        con = lead_db.connect()
        try:
            survivors = []
            for raw in items:
                item = {
                    "title": raw["title"],
                    "body": raw.get("body", ""),
                    "lead_id": raw.get("lead_id"),
                    "kind": raw.get("kind") or raw["title"],
                    "severity": raw.get("severity", "info"),
                }
                if _is_dupe(con, workspace_id, item):
                    counters["deduped"] += 1
                else:
                    survivors.append(item)
            if not survivors:
                return counters

            text = "\n\n".join(_format(i) for i in survivors)
            subject = survivors[0]["title"] if len(survivors) == 1 else \
                f"Replyworthy: {len(survivors)} alerts"

            if _telegram_send(text):
                channel = "telegram"
            elif _email_send(subject, text):
                channel = "email"
            else:
                channel = "stderr"
                print(f"ALERT (no channel configured):\n{text}", file=sys.stderr)

            # Independent of the owner chain: the workspace's own alert
            # email, when one is set (v2/06 D).
            _workspace_email_send(con, workspace_id, subject, text)

            with con.tx():
                for item in survivors:
                    con.execute(
                        "INSERT INTO alerts (workspace_id, lead_id, kind, severity, "
                        "title, body, channel, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (workspace_id, item["lead_id"], item["kind"],
                         item["severity"], item["title"], item["body"], channel,
                         db.utcnow()),
                    )
            counters["sent"] = len(survivors)
            counters["channel"] = channel
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - alerting must never break the pipeline
        print(f"  [notify] alert delivery crashed: {exc}", file=sys.stderr)
    return counters


def send_alert(workspace_id: int, title: str, body: str = "",
               lead_id: int | None = None, severity: str = "info",
               kind: str | None = None) -> dict:
    """One alert for one workspace. Operational alerts that belong to no
    tenant use lead_db.OWNER_WORKSPACE_ID (the owner sees ops noise)."""
    return send_batch(workspace_id, [{"title": title, "body": body,
                                      "lead_id": lead_id, "severity": severity,
                                      "kind": kind}])


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a test alert through the channel chain.")
    parser.add_argument("--test", action="store_true", required=True)
    args = parser.parse_args()
    result = send_alert(
        lead_db.OWNER_WORKSPACE_ID,
        "Test alert from Replyworthy",
        "If you can read this on your phone, Stage 5 alerting works.",
        severity="info", kind=f"test_{os.getpid()}",
    )
    print(f"Result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
