"""Billing + metering (V2 Stage 5, plans/v2/05): plans, the credit ledger,
and the Dodo Payments (Merchant of Record) integration.

ALL provider-specific code lives in this ONE module (vendor isolation, same
pattern as the B2B connector): signature verification, event-name mapping,
checkout-link building. A Dodo -> Paddle swap = this module's mapping plus
the .env keys; the ledger, status machine and caps never change.

The money model in one paragraph: workspaces pay monthly through the
provider's HOSTED checkout (we own no payment UI); paying grants monthly
lead credits (1 credit = one business lead sourced+enriched, 2 = one person
lead); every pipeline run checks the balance BEFORE vendor money is spent
and debits when the delivered leads are recorded. A workspace that stops
paying flips to past_due/canceled: read-only, sending paused, nothing
deleted. The owner's own workspace (plan 'internal') is unmetered.

LIVE SETUP PENDING (2026-07-11): the owner has not opened the Dodo Payments
account yet. Everything here is built and tested offline against fixture
events. When the account exists, verify every spot marked VERIFY-AT-SETUP
against the Dodo dashboard before going live.

.env keys (all absent until the account exists; the UI says so honestly):
  DODO_API_KEY, DODO_WEBHOOK_SECRET, DODO_ENV=test|live,
  DODO_PRODUCT_STARTER / DODO_PRODUCT_GROWTH / DODO_PRODUCT_SCALE
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import lead_db  # noqa: E402

PROVIDER = "dodo"

# --- Plans (plans/v2/05 A; prices set against plans/v2/07 C) ------------------
# Tier differentiation (pricing revision 2026-07-23, supersedes the 07-22
# "credits only" shape): plans differ in CREDITS plus four SOFTWARE-ENFORCED
# capability limits — person_leads, max_offers, max_campaigns,
# rollover_multiple. Nothing else. Two kinds of promise stay banned on every
# tier: human labor (priority support, review calls — one operator cannot
# grade support) and "unlimited sending" (mail providers won't honor it).
# search_cap and daily_send_cap remain abuse rails held IDENTICAL across paid
# plans — they are not tier features and must never be advertised as such.
#
# monthly_credits: granted on activation and on every renewal.
# person_leads: whether B2B person search (2-credit leads, paid vendor API) is
#   available. Gated to growth+ on purpose: it is the thin-margin path, and the
#   persona split matches — starter is one business pitching itself, growth is
#   consultants/agencies. Enforced in web/app.py before the b2b_ready() check.
# max_offers / max_campaigns: how many non-deleted offer profiles / ACTIVE
#   campaigns a workspace may hold (None = unlimited). Enforced at the create
#   routes in web/app.py. Starter's 1 offer IS its tagline ("one offer, one
#   steady pipeline") made literal.
# rollover_multiple: unused credits carry one month, capped at this multiple of
#   the monthly grant (grant_monthly below). Scale gets 2x as a real perk.
# search_cap: server-side ceiling on one search's lead count. The credit balance
#   is the real limit on volume; this only stops one runaway search.
# daily_send_cap: per-workspace sends/day, an anti-abuse ceiling only. Real
#   sending speed is governed by the warmup ramp and the customer's own mailbox
#   caps (tools/mailboxes.py RAMP + per-mailbox cap, lower always wins). Never
#   set a paid plan to None: "unlimited sending" is a promise the mail providers
#   will not honor (Google hard-caps 2k/day/mailbox; safe cold volume is 20-50).
#
# THE LADDER MUST STAY MONOTONIC, in both money and capability:
#   - price_usd / monthly_credits strictly DECREASES up the tiers
#     (starter $0.395/credit -> growth $0.358 -> scale $0.316), so upgrading
#     always lowers the cost per lead. It inverted once (scale dearer than
#     growth) and nobody had a rational reason to upgrade.
#   - capability limits never DECREASE up the tiers (None = unlimited).
# tests/test_billing.py::PricingLadderTestCase asserts both — if a number
# change makes it fail, the pricing is broken, not the test.
#
# price_usd is what the customer is CHARGED today, i.e. the launch-offer price.
# The landing page states the post-launch price ($99 / $199 / $399) as a FUTURE
# rise, never as a struck-through "was" price — the product was never sold at
# those, and a false former-price claim is an FTC problem. Those list prices are
# marketing only and deliberately live nowhere in code.
PLANS = {
    "trial": {"label": "Trial", "price_usd": 0, "monthly_credits": 25,
              "search_cap": 25, "daily_send_cap": 20,
              "person_leads": False, "max_offers": 1, "max_campaigns": 1,
              "rollover_multiple": 1},
    "starter": {"label": "Starter", "price_usd": 79, "monthly_credits": 200,
                "search_cap": 500, "daily_send_cap": 1000,
                "person_leads": False, "max_offers": 1, "max_campaigns": 3,
                "rollover_multiple": 1},
    "growth": {"label": "Growth", "price_usd": 179, "monthly_credits": 500,
               "search_cap": 500, "daily_send_cap": 1000,
               "person_leads": True, "max_offers": 5, "max_campaigns": 15,
               "rollover_multiple": 1},
    "scale": {"label": "Scale", "price_usd": 379, "monthly_credits": 1200,
              "search_cap": 500, "daily_send_cap": 1000,
              "person_leads": True, "max_offers": None, "max_campaigns": None,
              "rollover_multiple": 2},
    # The owner's workspace: unmetered, never touches the provider.
    "internal": {"label": "Internal", "price_usd": 0, "monthly_credits": 0,
                 "search_cap": 500, "daily_send_cap": None,
                 "person_leads": True, "max_offers": None,
                 "max_campaigns": None, "rollover_multiple": 1},
}
PAID_PLANS = ("starter", "growth", "scale")
UNMETERED_PLANS = ("internal",)

TRIAL_DAYS = 14

# Credit cost per DELIVERED lead (excluded/filtered leads cost the customer
# nothing). Person costs us more (plans/v2/00 §6); the ratio is a config
# constant — tune it against real vendor invoices.
#
# MUST STAY DISCLOSED. A customer who buys 200 credits expecting 200 leads and
# gets 100 has a fair refund argument, so the 2-credit person rate is stated in
# the customer's own words on landing/index.html (pricing section), in
# landing/terms.html §4, and on the in-app billing page. Change this ratio and
# you must change all three in the same commit.
CREDIT_COST = {"business": 1, "person": 2}


class BillingBlocked(Exception):
    """A billing rule stops this action; str(exc) is the user-facing message."""


def plan_info(plan: str | None) -> dict:
    return PLANS.get(plan or "trial", PLANS["trial"])


# --- Ledger -------------------------------------------------------------------


def init_workspace_billing(con: db.DB, workspace_id: int) -> None:
    """One-time trial grant + expiry for a NEW workspace. Runs inside
    create_workspace's transaction so a workspace never exists half-billed."""
    grant = PLANS["trial"]["monthly_credits"]
    con.execute(
        "INSERT INTO credit_ledger (workspace_id, delta, reason, ref, "
        "created_at) VALUES (?, ?, 'trial_grant', 'signup', ?)",
        (workspace_id, grant, db.utcnow()),
    )
    con.execute(
        "UPDATE workspaces SET credits_balance = ?, trial_expires_at = ? "
        "WHERE id = ?",
        (grant, db.utcnow_plus(days=TRIAL_DAYS), workspace_id),
    )


def add_ledger(con: db.DB, workspace_id: int, delta: int, reason: str,
               ref: str | None = None) -> None:
    """One ledger row plus the cached balance on the workspace row. Callers
    wrap in con.tx() so row and cache can never drift."""
    con.execute(
        "INSERT INTO credit_ledger (workspace_id, delta, reason, ref, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, delta, reason, ref, db.utcnow()),
    )
    con.execute(
        "UPDATE workspaces SET credits_balance = credits_balance + ? "
        "WHERE id = ?",
        (delta, workspace_id),
    )


def balance(workspace_id: int, con: db.DB | None = None) -> int:
    own = con is None
    con = con or lead_db.connect()
    try:
        row = con.execute(
            "SELECT credits_balance FROM workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        return (row["credits_balance"] or 0) if row else 0
    finally:
        if own:
            con.close()


def ledger_rows(workspace_id: int, limit: int = 20) -> list[dict]:
    con = lead_db.connect()
    try:
        rows = con.execute(
            "SELECT * FROM credit_ledger WHERE workspace_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def grant_monthly(con: db.DB, workspace_id: int, plan: str, ref: str) -> None:
    """Monthly grant with capped rollover (plans/v2/05 A): unused credits
    carry forward one month, at most rollover_multiple x the monthly grant
    (1x on most plans, 2x on scale — a real tier perk, 2026-07-23) — anything
    above the cap expires first, then the fresh grant lands."""
    info = plan_info(plan)
    grant = info["monthly_credits"]
    cap = grant * info["rollover_multiple"]
    current = balance(workspace_id, con)
    if current > cap:
        add_ledger(con, workspace_id, -(current - cap), "rollover_expiry", ref)
    add_ledger(con, workspace_id, grant, "monthly_grant", ref)


def debit_leads(workspace_id: int, n_business: int, n_person: int,
                ref: str | None = None) -> int:
    """Debit one finished run's DELIVERED leads. Returns credits charged
    (0 on unmetered plans). Runs after the work is recorded — a batch may
    briefly push the balance negative (plans/v2/05 D: accepted; the next
    pre-run check hard-stops)."""
    ws = lead_db.get_workspace(workspace_id)
    if ws is None or ws["plan"] in UNMETERED_PLANS:
        return 0
    total = 0
    con = lead_db.connect()
    try:
        with con.tx():
            if n_business:
                cost = n_business * CREDIT_COST["business"]
                add_ledger(con, workspace_id, -cost, "lead_business", ref)
                total += cost
            if n_person:
                cost = n_person * CREDIT_COST["person"]
                add_ledger(con, workspace_id, -cost, "lead_person", ref)
                total += cost
    finally:
        con.close()
    return total


def adjust_credits(workspace_id: int, delta: int, note: str) -> None:
    """Admin-only manual correction (support goodwill, refund bookkeeping).
    Actual refunds happen in the provider's dashboard — they are the seller;
    our side is only ever an 'adjustment' ledger row (plans/v2/05 D)."""
    con = lead_db.connect()
    try:
        with con.tx():
            add_ledger(con, workspace_id, delta, "adjustment", (note or "")[:200])
    finally:
        con.close()


# --- Access rules (the status machine, plans/v2/05 B+C) -----------------------


def trial_expired(ws: dict) -> bool:
    if ws.get("plan") != "trial":
        return False
    expires = ws.get("trial_expires_at")
    return bool(expires) and expires < db.utcnow()


def account_block_reason(ws: dict | None) -> str | None:
    """Why this workspace may not START anything (search/enroll) right now;
    None = clear. Reading is never blocked here — past_due/canceled keep
    their data and their login (plans/v2/05 B: reversible, polite)."""
    if ws is None:
        return "Unknown workspace."
    if ws.get("frozen"):
        return "This workspace is frozen. Contact support to restore access."
    status = ws.get("status")
    if status == "past_due":
        return ("There's a payment issue on this workspace, so it's read-only "
                "until the subscription recovers. Nothing was deleted.")
    if status == "canceled":
        return ("This subscription has ended and the workspace is read-only. "
                "Re-subscribe on the Billing page to pick up where you left "
                "off; your data is intact.")
    if trial_expired(ws):
        return ("The free trial has ended. Upgrade on the Billing page to "
                "keep generating leads.")
    return None


def run_guard(workspace_id: int, count: int, entity_type: str | None) -> str | None:
    """The pre-run gate: status first, then credits — all BEFORE any vendor
    money is spent (plans/v2/05 A). None = clear to run."""
    ws = lead_db.get_workspace(workspace_id)
    reason = account_block_reason(ws)
    if reason:
        return reason
    if ws["plan"] in UNMETERED_PLANS:
        return None
    kind = "person" if entity_type == "person" else "business"
    needed = max(1, int(count or 1)) * CREDIT_COST[kind]
    have = ws.get("credits_balance") or 0
    if needed > have:
        return (f"Not enough credits: this search needs up to {needed} and "
                f"this workspace has {have}. Upgrade or top up on the "
                "Billing page.")
    return None


def search_cap(workspace_id: int) -> int:
    """Per-plan ceiling on one search's lead count, enforced server-side."""
    ws = lead_db.get_workspace(workspace_id)
    return plan_info(ws["plan"] if ws else None)["search_cap"]


def daily_send_cap(plan: str | None) -> int | None:
    return plan_info(plan)["daily_send_cap"]


def _ws_plan_info(workspace_id: int) -> dict:
    ws = lead_db.get_workspace(workspace_id)
    return plan_info(ws["plan"] if ws else None)


def plan_allows_person(workspace_id: int) -> bool:
    """Person leads (B2B search, 2-credit) are a growth+ capability
    (2026-07-23). Enforced in web/app.py BEFORE the b2b_ready() check, so a
    starter/trial workspace sees the upgrade message, not the setup one."""
    return bool(_ws_plan_info(workspace_id)["person_leads"])


def offer_limit(workspace_id: int) -> int | None:
    """Max non-deleted offer profiles for this workspace (None = unlimited).
    Enforced at /offers/create."""
    return _ws_plan_info(workspace_id)["max_offers"]


def campaign_limit(workspace_id: int) -> int | None:
    """Max ACTIVE campaigns for this workspace (None = unlimited).
    Enforced at /campaigns/create."""
    return _ws_plan_info(workspace_id)["max_campaigns"]


def workspace_can_send(ws: dict) -> bool:
    """Sequencer gate: may this workspace's scheduled emails go out this
    tick? Trial workspaces may tick but only ever rehearse — the dry-run
    flag is per-workspace since v2/06 and send_email.workspace_live()
    returns False for trial plans whatever their flags say, so a trial
    'send' never leaves the machine (plans/v2/05 A: trial is dry-run only)."""
    if ws.get("frozen"):
        return False
    if ws.get("plan") in UNMETERED_PLANS:
        return True
    if ws.get("status") != "active":
        return False
    if ws.get("plan") == "trial":
        return not trial_expired(ws)
    return True


def set_frozen(workspace_id: int, frozen: bool) -> None:
    """The admin's instant abuse brake (plans/v2/05 C) — reversible."""
    con = lead_db.connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE workspaces SET frozen = ? WHERE id = ?",
                (1 if frozen else 0, workspace_id),
            )
    finally:
        con.close()


# --- Dodo Payments (all provider-specific code below this line) ---------------


def provider_ready() -> bool:
    """True once the owner's Dodo account exists and its keys are in .env.
    Until then the Billing page says so honestly (decision V2-9); checkout
    buttons only appear when this is true."""
    return bool(os.environ.get("DODO_API_KEY")
                and os.environ.get("DODO_WEBHOOK_SECRET"))


def _product_id(plan: str) -> str | None:
    return os.environ.get(f"DODO_PRODUCT_{plan.upper()}") or None


def checkout_url(workspace_id: int, plan: str) -> str | None:
    """Hosted static checkout link for one plan, tagged with the workspace
    so the webhook can route the subscription back. VERIFY-AT-SETUP: confirm
    the URL shape and the metadata_* passthrough params in the Dodo dashboard
    when the account exists."""
    product = _product_id(plan)
    if not (provider_ready() and product):
        return None
    host = ("checkout.dodopayments.com"
            if os.environ.get("DODO_ENV", "test").strip().lower() == "live"
            else "test.checkout.dodopayments.com")
    return (f"https://{host}/buy/{product}?quantity=1"
            f"&metadata_workspace_id={workspace_id}&metadata_plan={plan}"
            f"&metadata_sig={metadata_sig(workspace_id, plan)}")


def metadata_sig(workspace_id, plan) -> str:
    """Signature over the workspace + plan stamped into a checkout link. The
    link is editable by the buyer, so the webhook only believes metadata that
    carries the matching signature."""
    secret = os.environ.get("DODO_WEBHOOK_SECRET", "").strip()
    return hmac.new(secret.encode(), f"{workspace_id}:{plan}".encode(),
                    hashlib.sha256).hexdigest()


# Seconds of webhook-timestamp skew accepted (replay guard).
SIGNATURE_TOLERANCE = 5 * 60


def verify_webhook(headers: dict, body: bytes) -> bool:
    """Standard-Webhooks signature check (Dodo follows that spec): HMAC-SHA256
    over '<id>.<timestamp>.<body>' with the whsec_ secret; the header carries
    space-separated 'v1,<base64>' candidates. Constant-time compare; stale
    timestamps rejected. A missing secret rejects everything — the endpoint
    is dead until the owner configures it, never open."""
    secret = os.environ.get("DODO_WEBHOOK_SECRET", "").strip()
    if not secret:
        return False
    msg_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signature_header = headers.get("webhook-signature", "")
    if not (msg_id and timestamp and signature_header):
        return False
    try:
        if abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE:
            return False
    except ValueError:
        return False
    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret[len("whsec_"):])
        except (ValueError, binascii.Error):
            return False
    else:
        key = secret.encode()
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()
    for candidate in signature_header.split():
        version, _, sig = candidate.partition(",")
        if version == "v1" and sig and hmac.compare_digest(sig, expected):
            return True
    return False


# Dodo event type -> our four normalized events (plans/v2/05 B). Anything
# not listed is logged raw and ignored (payment.succeeded is implied by
# subscription.active/renewed). VERIFY-AT-SETUP against the dashboard's
# event list when the account exists.
EVENT_MAP = {
    "subscription.active": "plan_activated",
    "subscription.renewed": "period_renewed",
    "subscription.on_hold": "payment_failed",
    "subscription.failed": "payment_failed",
    "subscription.cancelled": "subscription_canceled",
}


def _event_workspace(payload: dict, con: db.DB) -> tuple[int | None, str | None]:
    """(workspace_id, plan) for one event. Our checkout links stamp both into
    the subscription metadata; the stored subscription id is the fallback
    route for later events that might not echo metadata back."""
    data = payload.get("data") or {}
    meta = data.get("metadata") or {}
    # Metadata rides in a checkout link the buyer can edit: believe it only
    # with our signature on it.
    sig = str(meta.get("sig") or "")
    trusted = bool(sig) and hmac.compare_digest(
        sig.encode(),
        metadata_sig(meta.get("workspace_id"), meta.get("plan")).encode())
    workspace_id = None
    if trusted:
        try:
            workspace_id = int(meta.get("workspace_id"))
        except (TypeError, ValueError):
            pass
    # The product that was actually paid for decides the plan.
    plan = None
    product = data.get("product_id")
    for candidate in PAID_PLANS:
        if product and product == _product_id(candidate):
            plan = candidate
            break
    if plan is None and trusted and meta.get("plan") in PAID_PLANS:
        plan = meta.get("plan")
    if workspace_id is None:
        sub_id = data.get("subscription_id")
        if sub_id:
            row = con.execute(
                "SELECT id FROM workspaces WHERE provider_subscription_id = ?",
                (sub_id,),
            ).fetchone()
            workspace_id = row["id"] if row else None
    return workspace_id, plan


def process_webhook(headers: dict, body: bytes) -> tuple[int, str]:
    """The whole webhook endpoint: verify -> record raw (idempotency: every
    provider event id is processed exactly once, replays are no-ops) ->
    apply the normalized event. Returns (http_status, message). Anything the
    handler can't route is stored raw and answered 200 so the provider stops
    retrying — /admin/billing is the reconciliation surface."""
    if not verify_webhook(headers, body):
        return 400, "bad signature"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, "bad payload"
    event_type = payload.get("type") or ""
    event_id = headers.get("webhook-id") or payload.get("id") or ""
    if not event_id:
        return 400, "missing event id"

    con = lead_db.connect()
    try:
        with con.tx():
            dupe = con.execute(
                "SELECT 1 FROM billing_events WHERE provider = ? AND event_id = ?",
                (PROVIDER, event_id),
            ).fetchone()
            if dupe:
                return 200, "duplicate ignored"
            workspace_id, plan = _event_workspace(payload, con)
            con.execute(
                "INSERT INTO billing_events (provider, event_id, event_type, "
                "workspace_id, raw, processed_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (PROVIDER, event_id, event_type, workspace_id,
                 json.dumps(payload, ensure_ascii=False)[:20000],
                 db.utcnow(), db.utcnow()),
            )
            normalized = EVENT_MAP.get(event_type)
            if normalized is None:
                return 200, f"ignored ({event_type or 'unknown type'})"
            ws = None
            if workspace_id is not None:
                ws = con.execute(
                    "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
                ).fetchone()
            if ws is None:
                return 200, "no matching workspace — logged for reconciliation"
            if ws["id"] == lead_db.OWNER_WORKSPACE_ID or ws["plan"] == "internal":
                return 200, "owner workspace is never billed — logged"

            data = payload.get("data") or {}
            ref = f"{PROVIDER}:{event_id}"
            if normalized == "plan_activated":
                if plan is None:
                    return 200, ("activated but plan unknown — reconcile in "
                                 "/admin/billing")
                customer = data.get("customer") or {}
                con.execute(
                    "UPDATE workspaces SET plan = ?, status = 'active', "
                    "provider_customer_id = ?, provider_subscription_id = ?, "
                    "trial_expires_at = NULL WHERE id = ?",
                    (plan, customer.get("customer_id"),
                     data.get("subscription_id"), ws["id"]),
                )
                grant_monthly(con, ws["id"], plan, ref)
            elif normalized == "period_renewed":
                if ws["status"] == "canceled":
                    # A late renewal must not bring a canceled plan back.
                    return 200, ("renewal for a canceled subscription — "
                                 "logged for reconciliation")
                grant_monthly(con, ws["id"], ws["plan"], ref)
                # A renewal also recovers a past_due workspace.
                con.execute(
                    "UPDATE workspaces SET status = 'active' WHERE id = ?",
                    (ws["id"],),
                )
            elif normalized == "payment_failed":
                con.execute(
                    "UPDATE workspaces SET status = 'past_due' WHERE id = ?",
                    (ws["id"],),
                )
            elif normalized == "subscription_canceled":
                con.execute(
                    "UPDATE workspaces SET status = 'canceled' WHERE id = ?",
                    (ws["id"],),
                )
            return 200, f"ok ({normalized})"
    finally:
        con.close()
