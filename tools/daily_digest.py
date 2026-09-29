"""One daily Telegram message with yesterday's numbers — the habit loop.

Stage-5 digest (plans/05_STAGE5_ALERTS_DASHBOARD.md C). Everything too quiet
for an urgent alert (not_interested, OOO, single bounces, send counts) lands
here instead, so the urgent channel stays sacred.

maybe_send() is called by every outreach tick: it fires at most once per
calendar day, after DIGEST_HOUR in the OWNER's local time (the machine's
clock — laptop now, TZ env var on Render later). The sent-marker lives in
the data dir so restarts don't re-send.

Usage:
    python tools/daily_digest.py --send      # force one now (owner testing)
    python tools/daily_digest.py --preview   # print it, send nothing
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import lead_db  # noqa: E402
import notify  # noqa: E402
from config.settings import DATA_DIR  # noqa: E402

DIGEST_HOUR = 8  # owner-local
MARKER_FILE = DATA_DIR / "last_digest_date.txt"
QUEUE_STATUSES = ("interested", "needs_human")


def build_digest(workspace_id: int) -> str:
    """One workspace's numbers (the digest goes to the owner's phone with a
    section per workspace)."""
    yday = db.utctoday(days_offset=-1)
    con = lead_db.connect()
    try:
        sends_yday = con.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE workspace_id = ? "
            "AND status = 'sent' AND substr(sent_at, 1, 10) = ?",
            (workspace_id, yday),
        ).fetchone()["n"]
        replies_yday = con.execute(
            "SELECT classification, COUNT(*) AS n FROM replies "
            "WHERE workspace_id = ? AND substr(received_at, 1, 10) = ? "
            "GROUP BY classification ORDER BY n DESC",
            (workspace_id, yday),
        ).fetchall()
        # Same window the circuit breaker watches.
        recent = con.execute(
            "SELECT l.email_verified FROM sends s JOIN leads l ON l.id = s.lead_id "
            "WHERE s.workspace_id = ? AND s.status = 'sent' "
            "ORDER BY s.sent_at DESC LIMIT 50",
            (workspace_id,),
        ).fetchall()
        queue_n = con.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE workspace_id = ? "
            "AND status IN (?, ?)",
            (workspace_id, *QUEUE_STATUSES),
        ).fetchone()["n"]
    finally:
        con.close()

    lines = [f"Sends yesterday: {sends_yday}"]
    if replies_yday:
        lines.append("Replies yesterday: "
                     + ", ".join(f"{r['classification'] or '?'} {r['n']}" for r in replies_yday))
    else:
        lines.append("Replies yesterday: none")
    if recent:
        bounced = sum(1 for r in recent if r["email_verified"] == "bounced")
        lines.append(f"Bounce rate (last {len(recent)} sends): {bounced / len(recent):.0%}")
    counts = lead_db.count_by_status(workspace_id)
    if counts:
        lines.append("Pipeline: " + ", ".join(f"{s} {n}" for s, n in counts.items()))
    lines.append(f"Waiting in your queue: {queue_n}"
                 + (f" -> {notify._base_url()}/queue" if queue_n else ""))
    return "\n".join(lines)


def build_full_digest() -> str:
    """All workspaces, one section each (single-workspace installs read the
    same as before, no header)."""
    workspaces = lead_db.list_workspaces()
    if len(workspaces) == 1:
        return build_digest(workspaces[0]["id"])
    sections = []
    for ws in workspaces:
        sections.append(f"[{ws['name']}]\n{build_digest(ws['id'])}")
    return "\n\n".join(sections)


def send_now(today: str | None = None) -> dict:
    today = today or datetime.now().strftime("%Y-%m-%d")
    result = notify.send_alert(
        lead_db.OWNER_WORKSPACE_ID,
        f"Daily digest — {today}", build_full_digest(),
        severity="info", kind=f"digest_{today}",
    )
    MARKER_FILE.write_text(today, encoding="utf-8")
    return result


def maybe_send(now: datetime | None = None) -> bool:
    """Fire once per day after DIGEST_HOUR local. Called by every tick."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < DIGEST_HOUR:
        return False
    if MARKER_FILE.exists() and MARKER_FILE.read_text(encoding="utf-8").strip() == today:
        return False
    send_now(today)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily digest of outreach numbers.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--send", action="store_true", help="send it now")
    group.add_argument("--preview", action="store_true", help="print, don't send")
    args = parser.parse_args()

    if args.preview:
        print(build_full_digest())
        return 0
    print(f"Sent: {send_now()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
