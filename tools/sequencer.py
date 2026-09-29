"""Sequence scheduler — the tick that decides what gets sent, when, to whom.

Stage-2 brain (plans/02_STAGE2_OUTREACH_ENGINE.md C2). Each tick:
  1. Kill switch: OUTREACH_PAUSED=true or a PAUSED file in the data dir -> exit.
  2. Load due sends (status='scheduled', scheduled_at <= now).
  3. Enforce per send: lead still contactable, not suppressed, email mx_ok,
     inside the lead's local send window (Mon-Fri 8:00-17:00), per-mailbox
     daily cap.
  4. Send (tools/send_email.py — dry-run by default), rotate mailboxes,
     randomized spacing between real sends.
  5. On success: send row -> 'sent', lead -> 'contacted', schedule the next
     step (+delay_days with jitter). Follow-ups thread under step 1 via
     In-Reply-To and a "Re:" subject.
  6. Three consecutive mailbox failures -> stop the tick loudly (Stage 5
     turns this print into a phone alert).

During the build this runs on the laptop: `--tick` once, or `--loop` (tick
every 15 min). At final deploy the same `--tick` becomes a Render Cron Job.

Usage:
    python tools/sequencer.py --tick
    python tools/sequencer.py --loop
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.settings import DATA_DIR, require  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import lead_db  # noqa: E402
import mailboxes  # noqa: E402 - the Stage-6 tenant-mailbox module
import notify  # noqa: E402
from send_email import (dry_run_enabled, send_email, send_from_mailbox,  # noqa: E402
                        send_reply_env, send_reply_from_mailbox,
                        workspace_live)
from send_email import mailboxes as env_mailboxes  # noqa: E402

PAUSED_FILE = DATA_DIR / "PAUSED"
LOOP_INTERVAL = 15 * 60          # seconds between ticks in --loop mode
SPACING_RANGE = (30, 90)         # seconds between real sends (anti-burst)
MAX_CONSECUTIVE_FAILURES = 3
WINDOW_START, WINDOW_END = 8, 17  # lead-local send window, Mon-Fri
DEFAULT_ZONE = "America/Chicago"  # central-ish fallback when state unknown

# State -> primary IANA zone. Good enough for a send window; split-zone
# states get their majority zone.
STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "HI": "Pacific/Honolulu", "ID": "America/Boise",
    "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
    "DC": "America/New_York",
}

CONTACTABLE_STATUSES = ("queued", "contacted")


def is_paused() -> bool:
    if os.environ.get("OUTREACH_PAUSED", "").strip().lower() == "true":
        return True
    return PAUSED_FILE.exists()


def _lead_zone(city: str | None) -> ZoneInfo:
    """Timezone from the trailing state code of 'Boise ID' style city strings."""
    if city:
        m = re.search(r"\b([A-Za-z]{2})\s*$", city.strip())
        if m:
            zone = STATE_TZ.get(m.group(1).upper())
            if zone:
                return ZoneInfo(zone)
    return ZoneInfo(DEFAULT_ZONE)


def in_send_window(city: str | None, now_utc: datetime) -> bool:
    """Mon-Fri, 8:00-16:59 in the lead's local time."""
    local = now_utc.astimezone(_lead_zone(city))
    return local.weekday() < 5 and WINDOW_START <= local.hour < WINDOW_END


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sent_today_by_mailbox(con, workspace_id: int) -> dict[str, int]:
    """Today's sent counts per mailbox address, within ONE workspace —
    mailboxes belong to a workspace since v2/06."""
    rows = con.execute(
        "SELECT mailbox, COUNT(*) AS n FROM sends WHERE workspace_id = ? "
        "AND status = 'sent' AND substr(sent_at, 1, 10) = ? GROUP BY mailbox",
        (workspace_id, db.utctoday()),
    ).fetchall()
    return {r["mailbox"]: r["n"] for r in rows}


def _workspace_boxes(con, ws: dict, ws_live: bool) -> tuple[list[dict], bool]:
    """Sendable mailboxes for one workspace as [{'address','cap','row'}],
    plus a legacy flag. cap None = only the campaign cap binds.

    Priority (plans/v2/06 B): the workspace's own verified mailboxes (ramp
    cap applied). The owner workspace falls back to the V1 env mailboxes;
    a tenant in practice mode with nothing connected gets the rehearsal
    pseudo-box so trials can watch the machinery run."""
    rows = mailboxes.active_mailboxes(con, ws["id"])
    if rows:
        return ([{"address": r["address"], "cap": mailboxes.effective_cap(r),
                  "row": r} for r in rows], False)
    if ws["id"] == lead_db.OWNER_WORKSPACE_ID:
        env = env_mailboxes()
        if env:
            return [{"address": b, "cap": None, "row": None} for b in env], True
        if dry_run_enabled():
            return [{"address": "dryrun@localhost", "cap": None, "row": None}], True
        # Live with nothing configured: the owner's sends wait (never raise —
        # one workspace must not stop the others' tick, plans/v2/06 B).
        print("ALERT: owner workspace is live but SMTP_MAILBOXES is empty - "
              "its sends wait.", file=sys.stderr)
        return [], True
    if ws_live:
        return [], False  # live with no verified mailbox: sends wait
    return [{"address": "dryrun@localhost", "cap": None, "row": None}], False


def _pick_mailbox(boxes: list[dict], sent_today: dict[str, int],
                  campaign_cap: int) -> dict | None:
    """Least-used box still under min(its own cap, the campaign cap)."""
    under_cap = [
        b for b in boxes
        if sent_today.get(b["address"], 0)
        < (campaign_cap if b["cap"] is None else min(b["cap"], campaign_cap))
    ]
    if not under_cap:
        return None
    return min(under_cap, key=lambda b: sent_today.get(b["address"], 0))


def _footer_identity(con, ws: dict) -> str:
    """The name on a tenant workspace's CAN-SPAM footer and follow-up
    sign-offs: the default offer's sender_name, else the workspace name."""
    row = con.execute(
        "SELECT sender_name FROM offer_profiles WHERE workspace_id = ? "
        "AND is_default = 1 AND deleted = 0",
        (ws["id"],),
    ).fetchone()
    if row and (row["sender_name"] or "").strip():
        return row["sender_name"].strip()
    return ws.get("name") or ""


def _render_template(template: str, lead: dict) -> str:
    """Merge {name} {city} {category} {pain_point_1} into a step template.
    Missing values render as sensible fallbacks, never as literal braces."""
    pain_points = []
    if lead.get("pain_points"):
        try:
            pain_points = json.loads(lead["pain_points"])
        except (json.JSONDecodeError, TypeError):
            pain_points = []
    values = {
        "name": lead.get("name") or "there",
        "city": lead.get("city") or "your area",
        "category": lead.get("category") or "local business",
        "pain_point_1": pain_points[0] if pain_points else "",
    }
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    # A template line that only existed to show the pain point dies cleanly.
    return "\n".join(
        line for line in out.splitlines() if line.strip() not in ("P.S.", "P.S. .")
    ).strip()


def _signature(workspace_id: int, footer_name: str) -> str:
    """Follow-up sign-off. The owner workspace keeps its V1 env identity
    (name + phone); tenant workspaces sign with their own footer name —
    never with the platform's env identity."""
    if workspace_id == lead_db.OWNER_WORKSPACE_ID and os.environ.get("SENDER_NAME"):
        return f"\n\n{require('SENDER_NAME')}\n{require('SENDER_PHONE')}"
    return f"\n\n{footer_name}" if footer_name else ""


def _step1_message_id(con, workspace_id: int, lead_id: int,
                      campaign_id: int) -> str | None:
    row = con.execute(
        "SELECT message_id FROM sends WHERE workspace_id = ? AND lead_id = ? "
        "AND campaign_id = ? AND step_number = 1 AND status = 'sent' "
        "ORDER BY id DESC LIMIT 1",
        (workspace_id, lead_id, campaign_id),
    ).fetchone()
    return row["message_id"] if row else None


def _step1_subject(con, workspace_id: int, lead_id: int) -> str:
    row = con.execute(
        "SELECT email_subject FROM leads WHERE id = ? AND workspace_id = ?",
        (lead_id, workspace_id),
    ).fetchone()
    return (row["email_subject"] or "quick question") if row else "quick question"


def _schedule_next_step(con, send_row, now_utc: datetime) -> None:
    nxt = con.execute(
        "SELECT * FROM sequence_steps WHERE campaign_id = ? AND step_number = ?",
        (send_row["campaign_id"], send_row["step_number"] + 1),
    ).fetchone()
    if not nxt:
        return
    jitter = timedelta(hours=random.uniform(-6, 6))
    when = now_utc + timedelta(days=nxt["delay_days"]) + jitter
    con.execute(
        "INSERT INTO sends (workspace_id, lead_id, campaign_id, step_number, "
        "scheduled_at) VALUES (?, ?, ?, ?, ?)",
        (send_row["workspace_id"], send_row["lead_id"], send_row["campaign_id"],
         nxt["step_number"], _iso(when)),
    )


def _build_message(con, send_row, lead,
                   footer_name: str = "") -> tuple[str, str, str | None]:
    """Returns (subject, body, in_reply_to) for this send."""
    step = send_row["step_number"]
    if step == 1:
        # Step 1 = the per-lead Claude-personalized copy from Stage 1
        # (signature already appended by analyze_lead).
        return lead["email_subject"] or "quick question", lead["email_body"] or "", None

    step_def = con.execute(
        "SELECT * FROM sequence_steps WHERE campaign_id = ? AND step_number = ?",
        (send_row["campaign_id"], step),
    ).fetchone()
    workspace_id = send_row["workspace_id"]
    body = (_render_template(step_def["body_template"] or "", dict(lead))
            + _signature(workspace_id, footer_name))
    subject = "Re: " + _step1_subject(con, workspace_id, send_row["lead_id"])
    in_reply_to = _step1_message_id(con, workspace_id, send_row["lead_id"],
                                    send_row["campaign_id"])
    return subject, body, in_reply_to


def _reply_subject(lead: dict) -> str:
    """'Re: <original subject>', collapsing an existing Re: so it never doubles."""
    subj = (lead.get("email_subject") or "your message").strip()
    if subj.lower().startswith("re:"):
        return subj
    return f"Re: {subj}"


def send_manual_reply(workspace_id: int, lead_id: int, body: str) -> dict:
    """Send a HUMAN-written 1:1 reply to a lead who answered. The person typed
    every word and clicked send — this is not an auto-reply. It goes through
    the workspace's own mailbox, threads into the existing conversation, and
    carries no cold-email footer or unsubscribe headers (the prospect started
    this thread). Respects the practice/live gate and suppression, and stops
    the automated follow-ups because a human is now handling the lead.

    Returns {'sent': bool, 'live': bool, 'to': str|None, 'error': str|None}.
    Any problem (blank body, unknown/foreign/suppressed lead, no mailbox) sends
    nothing and comes back with 'error' set — the caller shows it and stops."""
    def fail(msg: str) -> dict:
        return {"sent": False, "live": False, "to": None, "error": msg}

    if not (body or "").strip():
        return fail("Type a reply first.")

    lead = lead_db.get_lead(workspace_id, lead_id)
    if not lead:
        return fail("Lead not found.")
    to_addr = (lead.get("email") or "").strip()
    if not to_addr:
        return fail("This lead has no email address on file.")

    con = lead_db.connect()
    try:
        if lead_db.is_suppressed(workspace_id, to_addr, con):
            return fail("This lead unsubscribed or is suppressed — no reply sent.")

        ws = lead_db.get_workspace(workspace_id)
        live = workspace_live(ws)
        boxes, legacy = _workspace_boxes(con, ws, live)
        # Drop the rehearsal pseudo-box in BOTH the owner (legacy) and tenant
        # (non-legacy) practice paths — a real reply needs a real mailbox, and
        # keeping it would dereference a None row on the tenant branch.
        real_boxes = [b for b in boxes
                      if not (b["row"] is None and b["address"] == "dryrun@localhost")]
        if not real_boxes:
            return fail("Connect a sending mailbox first (Mailboxes page).")
        box = real_boxes[0]

        # Thread into our last email to this lead, if we have one.
        anchor = con.execute(
            "SELECT message_id FROM sends WHERE workspace_id = ? AND lead_id = ? "
            "AND status = 'sent' AND message_id IS NOT NULL "
            "ORDER BY sent_at DESC LIMIT 1",
            (workspace_id, lead_id),
        ).fetchone()
        in_reply_to = anchor["message_id"] if anchor else None
        subject = _reply_subject(lead)

        try:
            if legacy:
                send_reply_env(box["address"], to_addr, subject, body,
                               in_reply_to=in_reply_to, live=live)
            else:
                creds = mailboxes.decrypt_creds(box["row"]["creds_encrypted"])
                send_reply_from_mailbox(box["row"], creds, to_addr, subject,
                                        body, in_reply_to=in_reply_to, live=live)
        except Exception as exc:  # noqa: BLE001 - surface the reason, send nothing
            return fail(f"Send failed: {exc}")

        # Record what was said and stop the automated bumps — a human owns this
        # conversation now. Both run whether live or practice, matching how the
        # sequencer advances lead state in practice mode.
        lead_db.add_note(workspace_id, lead_id, f"↩ Replied: {body}")
        lead_db.cancel_pending_sends(workspace_id, lead_id, con)
    finally:
        con.close()

    return {"sent": live, "live": live, "to": to_addr, "error": None}


def _finish_done_campaigns(con, workspace_id: int) -> None:
    """Active campaigns with no scheduled sends left -> 'done' + info alert."""
    finished = con.execute(
        "SELECT c.id, c.name FROM campaigns c WHERE c.workspace_id = ? "
        "AND c.status = 'active' "
        "AND EXISTS (SELECT 1 FROM sends s WHERE s.campaign_id = c.id "
        "            AND s.workspace_id = c.workspace_id) "
        "AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.campaign_id = c.id "
        "                AND s.workspace_id = c.workspace_id "
        "                AND s.status = 'scheduled')",
        (workspace_id,),
    ).fetchall()
    for c in finished:
        with con.tx():
            con.execute(
                "UPDATE campaigns SET status = 'done' "
                "WHERE id = ? AND workspace_id = ?",
                (c["id"], workspace_id),
            )
        notify.send_alert(
            workspace_id,
            f"Campaign finished — {c['name']}",
            "All steps sent or resolved. Numbers are in the daily digest.",
            severity="info", kind=f"campaign_done_{c['id']}",
        )


def tick(now_utc: datetime | None = None, spacing: tuple[int, int] | None = SPACING_RANGE) -> dict:
    """Process due sends once, one workspace at a time (plans/v2/04 B:
    nothing processes cross-workspace rows in one query). Returns counters
    aggregated across workspaces (for tests and the web UI)."""
    now_utc = now_utc or _utc_now()
    counters = {"sent": 0, "skipped_window": 0, "skipped_cap": 0,
                "cancelled": 0, "failed": 0, "skipped_billing": 0,
                "skipped_paused": 0, "skipped_no_mailbox": 0}

    if is_paused():
        print("PAUSED - tick doing nothing (remove data/PAUSED or OUTREACH_PAUSED).")
        return counters
    if dry_run_enabled():
        print("=== DRY RUN mode - no email leaves this machine ===")

    con = lead_db.connect()
    try:
        workspaces = con.execute(
            "SELECT * FROM workspaces ORDER BY id"
        ).fetchall()
        for ws_row in workspaces:
            ws = dict(ws_row)
            # Billing gate (plans/v2/05 B): frozen / past_due / canceled
            # workspaces send nothing; their sends stay scheduled and resume
            # untouched when the subscription recovers.
            if not billing.workspace_can_send(ws):
                counters["skipped_billing"] += 1
                continue
            # Per-workspace pause (plans/v2/06 C2): the bounce breaker or the
            # customer paused THIS workspace; everyone else keeps sending.
            if ws.get("outreach_paused"):
                counters["skipped_paused"] += 1
                continue
            try:
                _tick_workspace(con, ws, now_utc, spacing, counters)
            except Exception as exc:  # noqa: BLE001 - one workspace must not stop the rest
                counters["failed"] += 1
                print(f"  workspace {ws['id']} tick crashed: {exc}",
                      file=sys.stderr)
    finally:
        con.close()

    print(f"Tick done: {counters}")
    return counters


def _tick_workspace(con, ws: dict, now_utc: datetime,
                    spacing: tuple[int, int] | None, counters: dict) -> None:
    """Process one workspace's due sends through ITS OWN mailboxes
    (plans/v2/06 B): rotation and daily caps stay inside the workspace, the
    warmup ramp binds each mailbox, and whether anything really leaves the
    machine is the workspace's live switch under the global DRY_RUN master
    override. On top sits the plan's daily ceiling (plans/v2/05 C)."""
    workspace_id = ws["id"]
    consecutive_failures = 0
    due = con.execute(
        "SELECT s.*, c.daily_cap FROM sends s "
        "JOIN campaigns c ON c.id = s.campaign_id "
        "WHERE s.workspace_id = ? AND s.status = 'scheduled' "
        "AND s.scheduled_at <= ? AND c.status = 'active' "
        "ORDER BY s.scheduled_at",
        (workspace_id, _iso(now_utc)),
    ).fetchall()
    if not due:
        _finish_done_campaigns(con, workspace_id)  # last sends may have been cancelled
        return

    ws_live = workspace_live(ws)
    sent_today = _sent_today_by_mailbox(con, workspace_id)
    boxes, legacy = _workspace_boxes(con, ws, ws_live)
    footer_name = "" if legacy else _footer_identity(con, ws)
    ws_daily_cap = billing.daily_send_cap(ws.get("plan"))
    sent_ws_today = 0
    if ws_daily_cap is not None:
        sent_ws_today = con.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE workspace_id = ? "
            "AND status = 'sent' AND substr(sent_at, 1, 10) = ?",
            (workspace_id, db.utctoday()),
        ).fetchone()["n"]

    for send_row in due:
        lead = con.execute(
            "SELECT * FROM leads WHERE id = ? AND workspace_id = ?",
            (send_row["lead_id"], workspace_id),
        ).fetchone()

        # Lead moved on (replied/unsubscribed/bounced) -> this send dies.
        if (
            lead is None
            or lead["status"] not in CONTACTABLE_STATUSES
            or lead_db.is_suppressed(workspace_id, lead["email"], con)
            or lead["email_verified"] != "mx_ok"
        ):
            with con.tx():
                con.execute(
                    "UPDATE sends SET status = 'cancelled' "
                    "WHERE id = ? AND workspace_id = ?",
                    (send_row["id"], workspace_id),
                )
            counters["cancelled"] += 1
            continue

        # Outside the lead's business hours -> stays scheduled, next tick.
        if not in_send_window(lead["city"], now_utc):
            counters["skipped_window"] += 1
            continue

        # A live workspace with no verified mailbox: sends wait (the customer
        # was alerted when the mailbox broke / hasn't connected one yet).
        if not boxes:
            counters["skipped_no_mailbox"] += 1
            continue

        # Plan-level workspace ceiling first, then the per-mailbox caps
        # (warmup ramp + customer cap + campaign cap, lowest wins).
        if ws_daily_cap is not None and sent_ws_today >= ws_daily_cap:
            counters["skipped_cap"] += 1
            continue

        box = _pick_mailbox(boxes, sent_today, send_row["daily_cap"])
        if box is None:
            counters["skipped_cap"] += 1
            continue  # all mailboxes capped today; later sends same story but cheap to count

        subject, body, in_reply_to = _build_message(con, send_row, lead,
                                                    footer_name)
        try:
            if legacy:
                # Owner V1 path: env mailboxes, global DRY_RUN decides.
                message_id = send_email(
                    box["address"], lead["email"], subject, body,
                    lead_id=lead["id"], workspace_id=workspace_id,
                    in_reply_to=in_reply_to,
                )
            else:
                row = box["row"]
                live = ws_live and row is not None
                # Decrypt at the moment of use, only when really sending.
                creds = (mailboxes.decrypt_creds(row["creds_encrypted"])
                         if live else {})
                message_id = send_from_mailbox(
                    row or {"address": box["address"], "smtp_host": None,
                            "smtp_port": None},
                    creds, lead["email"], subject, body,
                    lead_id=lead["id"], workspace_id=workspace_id,
                    in_reply_to=in_reply_to, footer_name=footer_name,
                    postal_address=ws.get("postal_address") or "", live=live,
                )
        except Exception as exc:  # noqa: BLE001 - one bad send must not kill the tick
            consecutive_failures += 1
            mailbox_problem = _is_mailbox_failure(exc)
            with con.tx():
                if mailbox_problem:
                    # Nothing wrong with this email: it stays scheduled and
                    # goes out on a later tick, once a mailbox works.
                    con.execute(
                        "UPDATE sends SET error = ? "
                        "WHERE id = ? AND workspace_id = ?",
                        (str(exc)[:500], send_row["id"], workspace_id),
                    )
                else:
                    con.execute(
                        "UPDATE sends SET status = 'failed', error = ? "
                        "WHERE id = ? AND workspace_id = ?",
                        (str(exc)[:500], send_row["id"], workspace_id),
                    )
            counters["failed"] += 1
            print(f"  FAILED send #{send_row['id']}: {exc}", file=sys.stderr)
            if mailbox_problem and not legacy and box["row"] is not None:
                # The tenant's mailbox is broken (rotated password, provider
                # block): flip it to error, stop using it this tick, and tell
                # the CUSTOMER what to fix (plans/v2/06 D).
                mailboxes.mark_error(workspace_id, box["row"]["id"],
                                     str(exc)[:200], con)
                boxes = [b for b in boxes if b["address"] != box["address"]]
                notify.send_alert(
                    workspace_id,
                    f"Mailbox {box['address']} stopped working",
                    "The email provider refused it (usually a changed "
                    "password). Sending from this mailbox is paused until "
                    "you reconnect it on the Mailboxes page.",
                    severity="urgent", kind=f"mailbox_error_{box['row']['id']}",
                )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(
                    "ALERT: 3 consecutive send failures - stopping this tick. "
                    "Check mailbox credentials.", file=sys.stderr,
                )
                notify.send_alert(
                    workspace_id,
                    "Sending broken — 3 failures in a row, tick stopped",
                    f"Last error: {str(exc)[:200]}\nCheck mailbox app "
                    "passwords / SMTP settings. Sends retry next tick.",
                    severity="urgent", kind="mailbox_failure",
                )
                break
            continue

        consecutive_failures = 0
        with con.tx():
            con.execute(
                "UPDATE sends SET status = 'sent', sent_at = ?, mailbox = ?, "
                "message_id = ? WHERE id = ? AND workspace_id = ?",
                (_iso(now_utc), box["address"], message_id, send_row["id"],
                 workspace_id),
            )
            con.execute(
                "UPDATE leads SET status = 'contacted', updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND status = 'queued'",
                (db.utcnow(), lead["id"], workspace_id),
            )
            _schedule_next_step(con, send_row, now_utc)
        sent_today[box["address"]] = sent_today.get(box["address"], 0) + 1
        sent_ws_today += 1
        counters["sent"] += 1

        if spacing and ws_live:
            time.sleep(random.uniform(*spacing))

    _finish_done_campaigns(con, workspace_id)


def _is_mailbox_failure(exc: Exception) -> bool:
    """True when the MAILBOX is what broke (login refused, server unreachable,
    stored login unreadable). A refused recipient or a rejected message is a
    problem with that one email and must not take a healthy mailbox offline."""
    import smtplib
    from cryptography.fernet import InvalidToken

    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError)):
        return False
    # Every smtplib error is an OSError, so are timeouts and refused sockets.
    return isinstance(exc, (OSError, InvalidToken))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the outreach sequencer.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tick", action="store_true", help="process due sends once")
    group.add_argument("--loop", action="store_true",
                       help=f"tick every {LOOP_INTERVAL // 60} min until Ctrl+C")
    args = parser.parse_args()

    if args.tick:
        tick()
        return 0
    while True:
        tick()
        print(f"(sleeping {LOOP_INTERVAL // 60} min - Ctrl+C to stop)")
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
