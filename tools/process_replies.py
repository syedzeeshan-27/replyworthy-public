"""Turn classified replies into pipeline actions — the agent's judgment step.

Stage-3 auto-actions (plans/03_STAGE3_REPLY_HANDLING.md C). For every
unprocessed reply: classify it (Claude, unless the inbox reader already
classified it mechanically), then act:

    any human reply    cancel the lead's remaining scheduled sends
    interested         lead -> interested                       (alert later)
    not_interested     lead -> not_interested + suppress
    unsubscribe        lead -> unsubscribed + suppress
    needs_human        lead -> needs_human                      (alert later)
    out_of_office      keep status; push pending sends +10 days
    wrong_person       lead -> not_interested + suppress; but if the reply
                       contains a different email address, needs_human instead
                       (a referral is worth a human look)
    bounce             lead -> bounced + suppress + email_verified='bounced'

Safety valve: any classification with confidence != high is ACTED ON as
needs_human (stored classification stays what Claude said). Wrong auto-action
on a real prospect costs more than one notification.

Circuit breaker: bounces > 8% of a workspace's last 50 sent emails -> pause
THAT workspace's sending (workspaces.outreach_paused, per v2/06 C2 — other
workspaces keep sending) and shout. Protects the sending domain automatically.

Idempotent: acts only on replies with processed_at IS NULL, sets it after.
V1 never auto-replies to anyone — it classifies, stops sequences, and hands
humans a queue. That's the product promise.

Usage:
    python tools/process_replies.py --process
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import lead_db  # noqa: E402
import notify  # noqa: E402

BOUNCE_RATE_LIMIT = 0.08
BOUNCE_RATE_WINDOW = 50   # last N sent emails
OOO_PUSH_DAYS = 10

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _default_classifier(payload: dict) -> dict:
    from classify_reply import classify_reply  # lazy: Claude only when needed
    return classify_reply(payload)


def _contains_other_email(body: str, own_email: str | None) -> bool:
    for m in EMAIL_RE.finditer(body or ""):
        if m.group(0).lower() != (own_email or "").lower():
            return True
    return False


def _push_pending_sends(con, workspace_id: int, lead_id: int, days: int) -> None:
    # Per-row update (not SQL date math) so the same code runs on both engines.
    rows = con.execute(
        "SELECT id, scheduled_at FROM sends WHERE workspace_id = ? "
        "AND lead_id = ? AND status = 'scheduled'",
        (workspace_id, lead_id),
    ).fetchall()
    for r in rows:
        con.execute(
            "UPDATE sends SET scheduled_at = ? WHERE id = ? AND workspace_id = ?",
            (db.shift_timestamp(r["scheduled_at"], days), r["id"], workspace_id),
        )


def _apply_actions(con, workspace_id: int, reply, lead, effective: str) -> None:
    lead_id = lead["id"] if lead else None
    email = lead["email"] if lead else None

    if lead_id is None:
        return  # unmatched reply: nothing to act on, human sees it in the queue

    if effective == "out_of_office":
        _push_pending_sends(con, workspace_id, lead_id, OOO_PUSH_DAYS)
        return  # status untouched - they're just away

    # A human answered (or the address is dead): the sequence is over.
    lead_db.cancel_pending_sends(workspace_id, lead_id, con)

    if effective == "interested":
        _set_status(con, workspace_id, lead_id, "interested", reply["summary"])
    elif effective == "not_interested":
        _set_status(con, workspace_id, lead_id, "not_interested", reply["summary"])
        if email:
            _suppress(con, workspace_id, email, "not_interested")
    elif effective == "unsubscribe":
        _set_status(con, workspace_id, lead_id, "unsubscribed", reply["summary"])
        if email:
            # An explicit unsubscribe blocks EVERY workspace (plans/v2/04 B).
            _suppress(con, workspace_id, email, "unsubscribed")
            _hard_suppress(con, email, "unsubscribed")
    elif effective == "needs_human":
        _set_status(con, workspace_id, lead_id, "needs_human", reply["summary"])
    elif effective == "bounce":
        _set_status(con, workspace_id, lead_id, "bounced", reply["summary"])
        con.execute(
            "UPDATE leads SET email_verified = 'bounced' "
            "WHERE id = ? AND workspace_id = ?",
            (lead_id, workspace_id),
        )
        if email:
            _suppress(con, workspace_id, email, "bounced")


def _set_status(con, workspace_id: int, lead_id: int, status: str,
                reason: str | None) -> None:
    con.execute(
        "UPDATE leads SET status = ?, status_reason = ?, updated_at = ? "
        "WHERE id = ? AND workspace_id = ?",
        (status, (reason or "")[:300], db.utcnow(), lead_id, workspace_id),
    )


def _suppress(con, workspace_id: int, email: str, reason: str) -> None:
    con.execute(
        "INSERT INTO suppression (workspace_id, email, reason, added_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (workspace_id, email.strip().lower(), reason, db.utcnow()),
    )


def _hard_suppress(con, email: str, reason: str) -> None:
    con.execute(
        "INSERT INTO hard_suppression (email, reason, added_at) "
        "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (email.strip().lower(), reason, db.utcnow()),
    )


def _bounce_circuit_breaker(con, workspace_id: int) -> bool:
    """True (and THIS workspace paused) when its recent bounce rate crosses
    the limit. Per-workspace since v2/06 C2: workspace A tripping never
    touches workspace B; the PAUSED file stays the global admin kill switch."""
    recent = con.execute(
        "SELECT l.email_verified FROM sends s JOIN leads l ON l.id = s.lead_id "
        "WHERE s.workspace_id = ? AND s.status = 'sent' "
        "ORDER BY s.sent_at DESC LIMIT ?",
        (workspace_id, BOUNCE_RATE_WINDOW),
    ).fetchall()
    if len(recent) < 10:  # too little data to judge
        return False
    bounced = sum(1 for r in recent if r["email_verified"] == "bounced")
    rate = bounced / len(recent)
    if rate > BOUNCE_RATE_LIMIT:
        con.execute(
            "UPDATE workspaces SET outreach_paused = 1 WHERE id = ?",
            (workspace_id,),
        )
        print(
            f"ALERT: bounce rate {rate:.0%} > {BOUNCE_RATE_LIMIT:.0%} - "
            f"workspace {workspace_id} AUTO-PAUSED to protect its domain. "
            "Fix the list before resuming.",
            file=sys.stderr,
        )
        return True
    return False


def process_pending(classifier=None) -> dict:
    """Classify + act on all unprocessed replies, one workspace at a time.
    Returns counters. `classifier` is injectable for tests; defaults to the
    Claude call."""
    classifier = classifier or _default_classifier
    counters = {"processed": 0, "alerts": 0, "auto_paused": False}
    # One phone batch per workspace, delivered after its replies are handled.
    per_ws_alerts: dict[int, list[dict]] = {}

    con = lead_db.connect()
    try:
        for workspace_id in lead_db.workspace_ids(con):
            alert_items = per_ws_alerts.setdefault(workspace_id, [])
            pending = con.execute(
                "SELECT * FROM replies WHERE workspace_id = ? "
                "AND processed_at IS NULL ORDER BY id",
                (workspace_id,),
            ).fetchall()

            for reply in pending:
                lead = None
                if reply["lead_id"]:
                    lead = con.execute(
                        "SELECT * FROM leads WHERE id = ? AND workspace_id = ?",
                        (reply["lead_id"], workspace_id),
                    ).fetchone()

                # Classify unless the reader already did (bounce / unmatched).
                if reply["classification"]:
                    result = {
                        "classification": reply["classification"],
                        "confidence": reply["confidence"] or "high",
                        "summary": reply["summary"] or "",
                        "suggested_action": reply["suggested_action"] or "",
                    }
                else:
                    our = None
                    if reply["send_id"]:
                        our = con.execute(
                            "SELECT s.step_number, l.email_subject, l.email_body "
                            "FROM sends s JOIN leads l ON l.id = s.lead_id "
                            "WHERE s.id = ? AND s.workspace_id = ?",
                            (reply["send_id"], workspace_id),
                        ).fetchone()
                    result = classifier({
                        "reply_text": reply["body_text"],
                        "our_subject": our["email_subject"] if our else "",
                        "our_body": our["email_body"] if our else "",
                        "name": lead["name"] if lead else "",
                        "category": lead["category"] if lead else "",
                        "city": lead["city"] if lead else "",
                    })

                classification = result["classification"]
                effective = classification
                # Low confidence -> a human decides, whatever Claude guessed.
                if classification != "bounce" and result["confidence"] != "high":
                    effective = "needs_human"
                # A referral inside a wrong_person reply deserves human eyes.
                if classification == "wrong_person" and effective != "needs_human":
                    if _contains_other_email(reply["body_text"], lead["email"] if lead else None):
                        effective = "needs_human"
                    else:
                        effective = "not_interested"

                with con.tx():
                    _apply_actions(con, workspace_id,
                                   dict(result, **{"summary": result["summary"]}),
                                   lead, effective)
                    con.execute(
                        "UPDATE replies SET classification = ?, confidence = ?, "
                        "summary = ?, suggested_action = ?, processed_at = ? "
                        "WHERE id = ? AND workspace_id = ?",
                        (classification, result["confidence"], result["summary"],
                         result["suggested_action"], db.utcnow(),
                         reply["id"], workspace_id),
                    )
                counters["processed"] += 1

                if effective in ("interested", "needs_human"):
                    counters["alerts"] += 1
                    who = lead["name"] if lead else reply["from_email"]
                    print(f"ALERT [{effective}] {who}: {result['summary']}")
                    where = f" ({lead['category']}, {lead['city']})" if lead else ""
                    body = result["summary"] or "(no summary)"
                    if result["suggested_action"]:
                        body += f"\n-> {result['suggested_action']}"
                    alert_items.append({
                        "title": f"{effective.upper()} — {who}{where}",
                        "body": body,
                        "lead_id": lead["id"] if lead else None,
                        "kind": effective,
                        "severity": "urgent",
                    })

            with con.tx():
                tripped = _bounce_circuit_breaker(con, workspace_id)
            if tripped:
                counters["auto_paused"] = True
                alert_items.append({
                    "title": "Sending AUTO-PAUSED — bounce rate over limit",
                    "body": "The circuit breaker paused this workspace's sending "
                            "to protect its domain. Fix the list, then resume "
                            "from the Sending page.",
                    "kind": "auto_paused",
                    "severity": "urgent",
                })
    finally:
        con.close()
        # In the finally on purpose: replies handled before a crash are
        # already marked processed, so this is the only chance to alert on them.
        for workspace_id, items in per_ws_alerts.items():
            if items:
                try:
                    notify.send_batch(workspace_id, items)
                except Exception:  # noqa: BLE001 - must not mask the real error
                    traceback.print_exc()

    print(f"Replies processed: {counters}")
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify and act on unprocessed replies.")
    parser.add_argument("--process", action="store_true", required=True)
    args = parser.parse_args()
    process_pending()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
