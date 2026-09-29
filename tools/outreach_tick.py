"""One outreach heartbeat: read replies -> classify+act -> send due emails.

The single command the scheduler runs (laptop loop now, Render Cron Job at
final deploy). Order matters: replies are read and acted on BEFORE sending,
so someone who answered five minutes ago never gets today's follow-up.

Each phase is isolated — an IMAP hiccup must not stop due sends, and vice
versa. Real failures print loudly (Stage 5 turns these into phone alerts).

Usage:
    python tools/outreach_tick.py            # one tick
    python tools/outreach_tick.py --loop     # tick every 15 min until Ctrl+C
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_digest  # noqa: E402
import lead_db  # noqa: E402
import notify  # noqa: E402
import sequencer  # noqa: E402
from ghl_sync import sync_changed_leads  # noqa: E402
from process_replies import process_pending  # noqa: E402
from read_inbox import poll_all  # noqa: E402

LOOP_INTERVAL = 15 * 60


def run_once() -> dict:
    """Run all phases. Returns {phase: result-or-'ERROR: ...'} for the web UI."""
    print("=== outreach tick ===")
    results: dict[str, object] = {}
    replies_ok = True
    for label, fn in (
        ("read inbox", poll_all),
        ("process replies", process_pending),
        ("send due emails", sequencer.tick),
        ("sync CRM", sync_changed_leads),  # no-op unless GHL_SYNC_ENABLED=true
        ("daily digest", daily_digest.maybe_send),  # fires once/day after 8am
    ):
        if label == "send due emails" and not replies_ok:
            # Unread replies may hold an unsubscribe or a "yes": sending a
            # follow-up over them is worse than sending 15 minutes late.
            results[label] = "SKIPPED: replies could not be checked this round"
            print("  send phase skipped: the reply phases failed")
            continue
        try:
            results[label] = fn()
        except Exception as exc:
            results[label] = f"ERROR: {exc}"
            if label in ("read inbox", "process replies"):
                replies_ok = False
            print(f"ALERT: tick phase '{label}' crashed:", file=sys.stderr)
            traceback.print_exc()
            # Operational alert — belongs to no tenant, the owner sees it.
            notify.send_alert(
                lead_db.OWNER_WORKSPACE_ID,
                f"Tick phase crashed — {label}",
                traceback.format_exc()[-600:],
                severity="warn", kind=f"tick_crash_{label}",
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one full outreach tick (or loop).")
    parser.add_argument("--loop", action="store_true",
                        help=f"repeat every {LOOP_INTERVAL // 60} min")
    args = parser.parse_args()

    run_once()
    while args.loop:
        print(f"(sleeping {LOOP_INTERVAL // 60} min - Ctrl+C to stop)")
        time.sleep(LOOP_INTERVAL)
        run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
