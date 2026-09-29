"""Create campaigns and enroll qualified leads into the email sequence.

Stage-2 enrollment (plans/02_STAGE2_OUTREACH_ENGINE.md C3). Two jobs:

1. --create-campaign: makes a campaign plus its default 3-step sequence
   (step 1 day 0 = the lead's own Claude-personalized email from Stage 1;
   step 2 +3 days = short bump; step 3 +4 more days = breakup). A campaign
   sells exactly one offer: the follow-up bodies are rendered from the
   campaign's offer profile (its value_prop) at creation time — never any
   tool or brand names. The offer comes from --offer-id, defaulting to the
   workspace's default offer.

2. --campaign <id>: enrolls sendable leads — status 'new', a real address
   (email_verified='mx_ok'), not suppressed, score >= --min-score — creating
   their step-1 send rows spread across days so the campaign respects the
   daily cap, and flipping the leads to status 'queued' (stamped with the
   campaign's offer_id).

Usage:
    python tools/enroll_leads.py --create-campaign "Boise dentists July" --daily-cap 30
    python tools/enroll_leads.py --create-campaign "Salons Aug" --offer-id 2
    python tools/enroll_leads.py --campaign 1 --min-score 4 --limit 50
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import billing  # noqa: E402
import db  # noqa: E402
import lead_db  # noqa: E402
from copy_guard import strip_dashes  # noqa: E402

# (step_number, delay_days_after_previous, body_template). Step 1 has no
# template - the lead's own generated copy is used. Offer-generic: the
# {value_prop} slot is filled from the campaign's offer profile when the
# campaign is created (build_default_steps); {name}/{city}/{category}/
# {pain_point_1} merge at send time; the signature is appended by the
# sequencer. Sign-off only - no name/phone in the template itself, and never
# a tool or brand name.
_FOLLOW_UP_TEMPLATES = [
    (1, 0, None),
    (
        2,
        3,
        "Hi {name} team,\n\n"
        "Floating my note back up in case it got buried. Short version: "
        "{value_prop}\n\n"
        "Worth a 2-minute video of how it works?\n\nBest,",
    ),
    (
        3,
        4,
        "Hi {name} team,\n\n"
        "Last note from me. If this isn't a headache right now, no worries "
        "at all - I'll close the file here.\n\n"
        "If it is, just reply - the short version: {value_prop} Happy to "
        "send a short video of how it works.\n\n"
        "Either way, thanks for reading.\n\nBest,",
    ),
]


def build_default_steps(offer: dict) -> list[tuple]:
    """Render the default 3-step sequence for one offer profile.

    Fills the {value_prop} slot from the offer now (the sequencer's send-time
    merge only knows {name}/{city}/{category}/{pain_point_1}, so anything
    offer-specific must be baked in before the templates hit the DB). Step
    numbers and day offsets are fixed; {name} stays for send time.
    """
    value_prop = (offer.get("value_prop") or "").strip()
    if value_prop and value_prop[-1] not in ".!?":
        value_prop += "."
    return [
        (
            step_number,
            delay_days,
            # strip_dashes: the offer's own value_prop may carry em dashes;
            # rendered follow-up copy must not (owner copy rule).
            strip_dashes(body.replace("{value_prop}", value_prop)) if body else None,
        )
        for step_number, delay_days, body in _FOLLOW_UP_TEMPLATES
    ]


def _resolve_offer(workspace_id: int, offer_id: int | None) -> dict:
    """Resolve the offer profile a campaign will sell.

    An explicit offer_id must exist in this workspace and not be soft-deleted;
    otherwise the workspace's default offer is used. Raises RuntimeError if
    neither is available.
    """
    if offer_id is not None:
        offer = lead_db.get_offer(workspace_id, offer_id)
        if offer is None or offer.get("deleted"):
            raise RuntimeError(f"No active offer profile with id {offer_id}.")
        return offer
    offer = lead_db.get_default_offer(workspace_id)
    if offer is None:
        raise RuntimeError(
            "No offer profile available: pass --offer-id or seed a default "
            "row in offer_profiles (tools/lead_db.py)."
        )
    return offer

# First touches go out at a random moment inside this UTC band, which maps to
# morning/afternoon across US timezones; the sequencer's send-window check is
# the final authority at send time.
FIRST_TOUCH_UTC_HOURS = (14, 22)


def _billing_gate(workspace_id: int) -> None:
    """Campaign work is a send-side action: a frozen / past_due / canceled /
    trial-expired workspace may read everything but not line up new outreach
    (plans/v2/05 B)."""
    reason = billing.account_block_reason(lead_db.get_workspace(workspace_id))
    if reason:
        raise billing.BillingBlocked(reason)


def create_campaign(workspace_id: int, name: str, daily_cap: int,
                    offer_id: int | None = None) -> int:
    _billing_gate(workspace_id)
    offer = _resolve_offer(workspace_id, offer_id)
    steps = build_default_steps(offer)
    con = lead_db.connect()
    try:
        with con.tx():
            campaign_id = con.insert_id(
                "INSERT INTO campaigns (workspace_id, name, status, daily_cap, "
                "offer_id, created_at) VALUES (?, ?, 'active', ?, ?, ?)",
                (workspace_id, name, daily_cap, offer["id"], db.utcnow()),
            )
            for step_number, delay_days, body_template in steps:
                con.execute(
                    "INSERT INTO sequence_steps "
                    "(campaign_id, step_number, delay_days, body_template) "
                    "VALUES (?, ?, ?, ?)",
                    (campaign_id, step_number, delay_days, body_template),
                )
        return campaign_id
    finally:
        con.close()


def enroll(workspace_id: int, campaign_id: int, min_score: int, limit: int) -> dict:
    """Queue step-1 sends for qualified leads. Returns counters."""
    _billing_gate(workspace_id)
    counters = {"enrolled": 0, "skipped_no_email": 0, "skipped_suppressed": 0}
    con = lead_db.connect()
    try:
        campaign = con.execute(
            "SELECT * FROM campaigns WHERE id = ? AND workspace_id = ?",
            (campaign_id, workspace_id),
        ).fetchone()
        if campaign is None:
            raise SystemExit(f"No campaign with id {campaign_id}.")

        # A campaign sells exactly one offer — stamp it on each enrolled lead
        # (campaigns.offer_id, falling back to the workspace default for
        # campaigns created before offers existed).
        offer = None
        if campaign["offer_id"]:
            offer = lead_db.get_offer(workspace_id, campaign["offer_id"])
        if offer is None:
            offer = lead_db.get_default_offer(workspace_id)
        stamp_offer_id = offer["id"] if offer else None

        candidates = con.execute(
            "SELECT * FROM leads WHERE workspace_id = ? AND status = 'new' "
            "AND score >= ? ORDER BY score DESC, id LIMIT ?",
            (workspace_id, min_score, limit),
        ).fetchall()

        daily_cap = max(1, campaign["daily_cap"] or 1)
        now = datetime.now(timezone.utc)
        with con.tx():
            for lead in candidates:
                if not lead["email"] or lead["email_verified"] != "mx_ok":
                    counters["skipped_no_email"] += 1
                    continue
                # Both tiers checked: the workspace's own list AND the global
                # hard list (plans/v2/04 B — enforced at enrollment).
                if lead_db.is_suppressed(workspace_id, lead["email"], con):
                    counters["skipped_suppressed"] += 1
                    continue

                day_offset = counters["enrolled"] // daily_cap
                hour = random.uniform(*FIRST_TOUCH_UTC_HOURS)
                when = (now + timedelta(days=day_offset)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(hours=hour)
                # Never schedule into the past on day 0.
                when = max(when, now)

                con.execute(
                    "INSERT INTO sends (workspace_id, lead_id, campaign_id, "
                    "step_number, scheduled_at) VALUES (?, ?, ?, 1, ?)",
                    (workspace_id, lead["id"], campaign_id,
                     when.strftime("%Y-%m-%d %H:%M:%S")),
                )
                con.execute(
                    "UPDATE leads SET status = 'queued', "
                    "offer_id = COALESCE(?, offer_id), updated_at = ? "
                    "WHERE id = ? AND workspace_id = ?",
                    (stamp_offer_id, db.utcnow(), lead["id"], workspace_id),
                )
                counters["enrolled"] += 1
    finally:
        con.close()
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Create campaigns / enroll leads.")
    parser.add_argument("--create-campaign", metavar="NAME",
                        help="create a campaign (+ default 3-step sequence) and print its id")
    parser.add_argument("--daily-cap", type=int, default=30)
    parser.add_argument("--offer-id", type=int, default=None,
                        help="offer_profiles id the campaign sells "
                             "(default: the workspace's default offer)")
    parser.add_argument("--campaign", type=int, help="campaign id to enroll leads into")
    parser.add_argument("--min-score", type=int, default=4)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--workspace-id", type=int, default=lead_db.OWNER_WORKSPACE_ID,
                        help="workspace to act in (CLI default: the owner workspace)")
    args = parser.parse_args()

    if args.create_campaign:
        campaign_id = create_campaign(args.workspace_id, args.create_campaign,
                                      args.daily_cap, args.offer_id)
        print(f"Created campaign {campaign_id}: {args.create_campaign!r} "
              f"(daily cap {args.daily_cap}, 3 steps)")
        return 0

    if args.campaign:
        counters = enroll(args.workspace_id, args.campaign, args.min_score, args.limit)
        print(
            f"Enrolled {counters['enrolled']} leads into campaign {args.campaign} "
            f"({counters['skipped_no_email']} without a verified email, "
            f"{counters['skipped_suppressed']} suppressed)."
        )
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
