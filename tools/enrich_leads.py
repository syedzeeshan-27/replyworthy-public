"""Enrich scraped leads with website analysis.

Pipeline orchestration step: takes the raw leads written by the source
connector (tools/sources/google_maps.py businesses, or
tools/sources/b2b_data_api.py persons — V2 Stage 3) and, for each one worth
pursuing, scrapes the relevant website (tools/scrape_website.py) and runs it
through Claude analysis (tools/analyze_lead.py), folding pain_points / score /
email copy back into the lead. Business leads keep the exact original path;
person leads skip widget detection and email mining (see enrich_person).

Behavior:
  - Reads the latest .tmp/maps_raw_<timestamp>.json (most recently modified).
  - Skips any lead with no website AND no email (nothing to act on).
  - Scrapes the website (if present), analyzes the result, and adds
    pain_points, score, and outreach_hook to the lead dict.
  - Sleeps 1s between leads to stay polite to the upstream APIs.
  - Writes the enriched leads to .tmp/leads_enriched_<timestamp>.json.

Usage:
    python tools/enrich_leads.py

This tool calls scrape_website() and analyze_lead() directly; it makes no
external API calls of its own. Those tools now run live (Jina/Firecrawl and the
Claude API respectively), so running this end-to-end spends real credits.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Make `config` and sibling tool modules importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root -> config
sys.path.insert(0, str(Path(__file__).resolve().parent))         # tools/ -> sibling tools
from config.settings import TMP_DIR  # noqa: E402
from scrape_website import scrape_website  # noqa: E402
from analyze_lead import analyze_lead  # noqa: E402
from detect_widgets import detect_existing_solutions  # noqa: E402
from extract_email import ensure_email  # noqa: E402
from extract_socials import ensure_socials  # noqa: E402
from verify_email import verify_email  # noqa: E402
from lead_db import OWNER_WORKSPACE_ID, get_default_offer, get_offer  # noqa: E402

SLEEP_BETWEEN_LEADS = 1  # seconds, to avoid hammering the scrape/analysis APIs


def _is_us_phone(phone: str | None) -> bool:
    """True if `phone` looks like a US/Canada (NANP) number.

    Accepts +1-prefixed numbers and bare 10-digit numbers; rejects other
    country codes. Used only when --us-only is set.
    """
    if not phone:
        return False
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+"):
        return digits.startswith("1") and len(digits) == 11
    # No explicit country code: treat a clean 10-digit number as US, or an
    # 11-digit number that starts with 1.
    if len(digits) == 10:
        return True
    return len(digits) == 11 and digits.startswith("1")


def latest_maps_raw() -> Path:
    """Return the most recently modified raw-leads payload in .tmp/ —
    maps_raw_* (Google Maps businesses) or b2b_raw_* (data-API persons,
    V2 Stage 3). The pipeline just wrote one of them; newest wins."""
    candidates = sorted(
        [*TMP_DIR.glob("maps_raw_*.json"), *TMP_DIR.glob("b2b_raw_*.json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No maps_raw_*.json / b2b_raw_*.json files in {TMP_DIR}. "
            "Run tools/scrape_google_maps.py (or the B2B connector) first."
        )
    return candidates[0]


def enrich_lead(lead: dict, soft_signals: list[str] | None = None, offer: dict | None = None) -> dict:
    """Scrape + analyze one lead, returning a new dict with the added fields."""
    website = lead.get("website")

    markdown = scrape_website(website) if website else None

    # Email capture: Apify's contact scrape is patchy; mine the page we just
    # fetched (plus at most one contact-page fetch) so outreach has an address.
    email, email_source = ensure_email(lead, markdown, website)
    email_verified = verify_email(email)

    # Social capture: actor-provided links win; the page we just fetched
    # fills any platform the contact crawl missed. No-email leads stay
    # reachable through a DM instead of going to waste.
    social_links = ensure_socials(lead, markdown)

    analysis = analyze_lead(
        {
            "name": lead.get("name"),
            "category": lead.get("category"),
            "city": lead.get("city"),
            "website": markdown,
            # CTA text found on the site with no matching software behind it —
            # pitch context for the analysis, not a disqualifier.
            "soft_signals": soft_signals or [],
        },
        offer,
    )

    enriched = dict(lead)
    enriched["email"] = email
    enriched["email_source"] = email_source
    enriched["email_verified"] = email_verified
    enriched["social_links"] = social_links
    enriched["soft_signals"] = soft_signals or []
    enriched["pain_points"] = analysis["pain_points"]
    enriched["score"] = analysis["score"]
    enriched["email_subject"] = analysis["email_subject"]
    enriched["email_body"] = analysis["email_body"]
    # analyze_lead sets copy_flagged only when a tool/brand name survived
    # regeneration; surface it on the enriched dict so it's visible for review.
    if analysis.get("copy_flagged"):
        enriched["copy_flagged"] = True
    if offer is not None:
        enriched["offer_id"] = offer["id"]
    return enriched


def enrich_person(lead: dict, offer: dict | None = None) -> dict:
    """Scrape + analyze one PERSON lead (V2 Stage 3, plan v2/03 C3).

    Persons differ from businesses on purpose:
    - Widget detection never runs — it answers a local-business question.
    - Email mining (ensure_email) never runs — a generic info@ scraped off
      the COMPANY site would land in the wrong inbox; the person's address
      comes from the data API or not at all (un-enrollable without one).
    - The free MX check still runs on the API-provided email — the vendor's
      "verified" is a claim, this is the second gate (plan v2/03 D).
    - The website scraped is the COMPANY site, as personalization evidence.
    """
    website = lead.get("website")
    markdown = scrape_website(website) if website else None

    email = lead.get("email")
    email_verified = verify_email(email)

    analysis = analyze_lead(
        {
            "entity_type": "person",
            "name": lead.get("name"),
            "title": lead.get("title"),
            "company": lead.get("company"),
            "category": lead.get("category"),
            "city": lead.get("city"),
            "website": markdown,
        },
        offer,
    )

    enriched = dict(lead)
    enriched["email_verified"] = email_verified
    enriched["pain_points"] = analysis["pain_points"]
    enriched["score"] = analysis["score"]
    enriched["email_subject"] = analysis["email_subject"]
    enriched["email_body"] = analysis["email_body"]
    if analysis.get("copy_flagged"):
        enriched["copy_flagged"] = True
    if offer is not None:
        enriched["offer_id"] = offer["id"]
    return enriched


def _resolve_offer(workspace_id: int, offer_id: int | None) -> dict:
    """Resolve the offer profile to analyze every lead in this run against.

    Resolved ONCE per run (not per-lead): an explicit --offer-id must exist
    in this workspace and not be soft-deleted, otherwise the workspace's
    default offer is used. Raises RuntimeError if neither is available.
    """
    if offer_id is not None:
        offer = get_offer(workspace_id, offer_id)
        if offer is None or offer.get("deleted"):
            raise RuntimeError(f"No active offer profile with id {offer_id}.")
        return offer
    offer = get_default_offer(workspace_id)
    if offer is None:
        raise RuntimeError(
            "No offer profile available: pass --offer-id or seed a default "
            "row in offer_profiles (tools/lead_db.py)."
        )
    return offer


def process_leads(leads: list[dict], offer: dict, us_only: bool = False) -> dict:
    """Run the qualification + enrichment loop over one batch of leads.

    Branches per lead on entity_type (V2 Stage 3): businesses keep the exact
    Stage-1 path (skip-no-contact → US filter → widget detect → enrich);
    persons skip widget detection and email mining entirely (see
    enrich_person) and land even without an email as long as there is SOME
    way to act on them (email, phone, LinkedIn, or a company site).
    The --us-only phone filter applies to businesses only — it exists for
    phone-centric local-business offers; person targeting is geo'd by the
    search spec itself.

    Returns {enriched, excluded_leads, skipped, excluded_existing,
    excluded_non_us} — the same counts the funnel report shows the owner.
    """
    enriched: list[dict] = []
    excluded_leads: list[dict] = []  # widget-excluded, persisted for audit
    skipped = 0            # nothing to act on (no website + no email)
    excluded_existing = 0  # already have chat/booking infrastructure
    excluded_non_us = 0    # non-US phone (only when --us-only)

    for i, lead in enumerate(leads, start=1):
        name = lead.get("name") or "(unnamed)"
        is_person = (lead.get("entity_type") or "business") == "person"

        if is_person:
            # A person with no contact info at all and no company site is
            # nothing we can act on; anything else lands (no email = shown
            # for manual use, un-enrollable until an address exists).
            if not (lead.get("email") or lead.get("phone")
                    or lead.get("linkedin_url") or lead.get("website")):
                skipped += 1
                print(f"[{i}/{len(leads)}] SKIP {name} - no way to contact or research")
                continue
            print(f"[{i}/{len(leads)}] Enriching {name} "
                  f"({lead.get('title') or '?'} at {lead.get('company') or '?'}) ...")
            result = enrich_person(lead, offer)
        else:
            # Nothing to act on without a website to scrape or an email to reach them.
            if not lead.get("website") and not lead.get("email"):
                skipped += 1
                print(f"[{i}/{len(leads)}] SKIP {name} - no website and no email")
                continue

            # Optional geography filter (bot only texts US/Canada numbers).
            if us_only and not _is_us_phone(lead.get("phone")):
                excluded_non_us += 1
                print(f"[{i}/{len(leads)}] EXCLUDE {name} - non-US phone ({lead.get('phone')})")
                continue

            # Pre-filter BEFORE any paid scrape/analysis: only HARD evidence of an
            # installed chat/booking product excludes; marketing copy alone doesn't.
            website = lead.get("website")
            soft_signals: list[str] = []
            if website:
                existing = detect_existing_solutions(website)
                soft_signals = existing.get("soft_signals", [])
                if existing["has_chat"] or existing["has_booking"]:
                    excluded_existing += 1
                    # Full lead dict + detection evidence: the audit file doubles
                    # as the source for status='excluded' rows in the lead DB.
                    excluded_leads.append({**lead, "detected": existing["detected"]})
                    print(
                        f"[{i}/{len(leads)}] EXCLUDE {name} - already has "
                        f"{existing['detected']}"
                    )
                    continue

            print(f"[{i}/{len(leads)}] Enriching {name} ...")
            result = enrich_lead(lead, soft_signals, offer)

        enriched.append(result)
        print(
            f"    score={result['score']}  "
            f"email={result.get('email') or '(none)'} "
            f"[{result.get('email_source')}/{result.get('email_verified')}]  "
            f"pain_points={result['pain_points']}"
        )

        # Be polite to the upstream scrape/analysis APIs.
        if i < len(leads):
            time.sleep(SLEEP_BETWEEN_LEADS)

    return {
        "enriched": enriched,
        "excluded_leads": excluded_leads,
        "skipped": skipped,
        "excluded_existing": excluded_existing,
        "excluded_non_us": excluded_non_us,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich scraped leads (Maps businesses or B2B persons).")
    parser.add_argument(
        "--us-only",
        action="store_true",
        help="keep only leads with a US/Canada (NANP) phone number (business leads)",
    )
    parser.add_argument(
        "--offer-id",
        type=int,
        default=None,
        help="offer_profiles id to analyze/score leads against (default: the workspace's default offer)",
    )
    parser.add_argument(
        "--workspace-id",
        type=int,
        default=OWNER_WORKSPACE_ID,
        help="workspace whose offers to resolve against (default: the owner workspace)",
    )
    parser.add_argument(
        "--in-file",
        default=None,
        help="raw-leads JSON to enrich (default: the most recent maps_raw_*/"
             "b2b_raw_* in .tmp). The pipeline passes an explicit path so it "
             "never reads another run's file.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="scopes the output filenames (leads_enriched_<run-id>.json and "
             "leads_excluded_<run-id>.json). Default: a fresh timestamp.",
    )
    args = parser.parse_args()

    offer = _resolve_offer(args.workspace_id, args.offer_id)
    print(f"Offer: {offer['name']} (id={offer['id']})")

    src_path = Path(args.in_file) if args.in_file else latest_maps_raw()
    payload = json.loads(src_path.read_text(encoding="utf-8"))
    leads = payload.get("results", [])
    print(f"Loaded {len(leads)} leads from {src_path}")
    if args.us_only:
        print("US-only filter: ON")

    outcome = process_leads(leads, offer, us_only=args.us_only)
    enriched = outcome["enriched"]
    excluded_leads = outcome["excluded_leads"]
    skipped = outcome["skipped"]
    excluded_existing = outcome["excluded_existing"]
    excluded_non_us = outcome["excluded_non_us"]

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = TMP_DIR / f"leads_enriched_{run_id}.json"
    out_payload = {
        "query": payload.get("query"),
        "source_file": src_path.name,
        "enriched_at": run_id,
        "count": len(enriched),
        "skipped": skipped,
        "excluded_existing": excluded_existing,
        "excluded_non_us": excluded_non_us,
        "results": enriched,
    }
    out_path.write_text(
        json.dumps(out_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Persist widget-excluded leads so exclusion reasons are auditable after the
    # run (previously they only existed in stdout). Same run id so run_lead_gen
    # can find the pair.
    if excluded_leads:
        excl_path = TMP_DIR / f"leads_excluded_{run_id}.json"
        excl_path.write_text(
            json.dumps(
                {"count": len(excluded_leads), "results": excluded_leads},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"Excluded-lead audit saved to {excl_path}")

    print(
        f"\nEnriched {len(enriched)} leads "
        f"({skipped} skipped, {excluded_existing} already-equipped, "
        f"{excluded_non_us} non-US). Saved to {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
