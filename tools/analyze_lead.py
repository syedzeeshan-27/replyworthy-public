"""Analyze a scraped business with Claude to score it as an outreach lead.

Pipeline node 4 ("Claude analyzes the site") + node 5 ("Score + personalize").
Given a business dict (name, category, city, and the website markdown from
tools/scrape_website.py) and an offer profile (tools/lead_db.py
offer_profiles table), ask Claude to surface pain points the offer fixes,
score the lead 1-5 relative to that offer's qualification hints and
disqualifiers, and write a full personalized cold outreach email (subject +
body). The sender signature is appended after generation — from the offer's
sender_name/sender_title if set, otherwise from SENDER_NAME/SENDER_PHONE in
.env — so the email is send-ready.

If the website markdown is None (scrape failed), we skip the API call and return
a low-confidence result (score 2, noting the site couldn't be scraped).

Usage (CLI, for testing one lead):
    python tools/analyze_lead.py --name "Bright Smile Dental" \
        --category "dentist" --city "Austin TX" --website-file .tmp/site_x.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Make `config` and `tools` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import LEADGEN_MODEL, require  # noqa: E402
from tools.copy_guard import check_copy, strip_dashes  # noqa: E402
from tools.lead_db import OWNER_WORKSPACE_ID, get_default_offer  # noqa: E402

MODEL = LEADGEN_MODEL
MAX_TOKENS = 2048
# Qualification signal (booking links, contact options, hours) lives in the
# header/hero/footer, not deep page content — cap what we send to Claude.
MAX_SITE_CHARS = 8000
RETRY_DELAY = 20  # seconds before the single app-level retry on 429/5xx

# Platform writing rules — fixed mechanics for every generated email,
# regardless of offer. Pitch content (what we sell, who it's for, how to
# qualify, tone, CTA style) is interpolated per-offer in _build_system_prompt.
_EMAIL_RULES = "[prompt text removed from the public copy of this repo]"

# Structured-output schema: the API guarantees the response is valid JSON
# matching this shape, so no fence-stripping/brace-hunting is needed.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "pain_points": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "email_subject": {"type": "string"},
        "email_body": {"type": "string"},
    },
    "required": ["pain_points", "score", "email_subject", "email_body"],
    "additionalProperties": False,
}

# Appended to every generated email so it comes out send-ready, when the
# offer itself doesn't specify a sender.
SENDER_NAME_KEY = "SENDER_NAME"
SENDER_PHONE_KEY = "SENDER_PHONE"


def _build_system_prompt(offer: dict, entity_type: str = "business") -> str:
    """The real prompt is built from the offer profile; its wording is
    not part of the public copy."""
    product = (offer.get("product_description") or "").strip()
    return "[prompt text removed from the public copy of this repo] Offer: " + product + " Entity: " + entity_type


_client = None


def _get_client():
    """Lazily construct (and cache) the Anthropic client.

    A module-level getter (rather than a plain module-level client) so tests
    can monkeypatch `tools.analyze_lead._get_client` with a fake object
    without needing a real ANTHROPIC_API_KEY or making a network call.
    """
    global _client
    if _client is None:
        from anthropic import Anthropic  # lazy import: only needed for live calls

        _client = Anthropic(api_key=require("ANTHROPIC_API_KEY"))
    return _client


def _sender_signature(offer: dict) -> str:
    """Return the '\\n\\n{name}\\n{title}' block to append to every email body.

    Uses the offer's sender_name (and sender_title, if set) when present;
    otherwise falls back to the SENDER_NAME/SENDER_PHONE settings exactly as
    before.
    """
    sender_name = (offer.get("sender_name") or "").strip()
    if sender_name:
        sender_title = (offer.get("sender_title") or "").strip()
        if sender_title:
            return f"\n\n{sender_name}\n{sender_title}"
        return f"\n\n{sender_name}"
    return f"\n\n{require(SENDER_NAME_KEY)}\n{require(SENDER_PHONE_KEY)}"


def _call_claude(business: dict, offer: dict, banned_terms: list[str] | None = None) -> dict:
    """Send the analysis prompt to Claude and return the parsed JSON result.

    `banned_terms`, when set, appends one instruction to the user prompt naming
    tool/brand names a prior draft leaked so the regeneration avoids them. The
    rest of the API call shape is identical regardless.
    """
    client = _get_client()
    site = (business.get("website") or "")[:MAX_SITE_CHARS]
    entity_type = business.get("entity_type") or "business"
    if entity_type == "person":
        user_prompt = (
            f"Person: {business.get('name')}\n"
            f"Job title: {business.get('title') or '(unknown)'}\n"
            f"Company: {business.get('company') or '(unknown)'}\n"
            f"Industry: {business.get('category') or '(unknown)'}\n"
            f"City: {business.get('city') or '(unknown)'}\n"
        )
        if site:
            user_prompt += f"\nCompany website content (markdown):\n{site}"
        else:
            user_prompt += (
                "\nNo company website content is available. Judge fit from "
                "the title, company, and industry alone, and score "
                "conservatively (less evidence = lower score)."
            )
    else:
        user_prompt = (
            f"Business name: {business.get('name')}\n"
            f"Category: {business.get('category')}\n"
            f"City: {business.get('city')}\n"
        )
        soft_signals = business.get("soft_signals") or []
        if soft_signals:
            user_prompt += (
                "Marketing text found on the site with NO matching booking/chat "
                f"software detected in its HTML: {', '.join(soft_signals)}\n"
            )
        user_prompt += f"\nWebsite content (markdown):\n{site}"
    if banned_terms:
        user_prompt += (
            "\n\nThe previous draft used these banned tool/brand names: "
            f"{', '.join(banned_terms)}. Do NOT use these or any other product, "
            "tool, brand, or company name. Describe the capability generically "
            "(e.g. \"a booking link\") instead."
        )

    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Sonnet 5 runs adaptive thinking when the field is omitted — disable
        # explicitly: this is a short scoring/copy task, thinking tokens are waste.
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=_build_system_prompt(offer, entity_type),
        messages=[{"role": "user", "content": user_prompt}],
    )
    if msg.stop_reason == "max_tokens":
        raise ValueError("response truncated at max_tokens")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def _call_with_retry(business: dict, offer: dict, banned_terms: list[str] | None = None) -> dict:
    """One app-level retry on rate-limit/5xx/network errors, on top of the
    SDK's built-in retries. Auth and other 4xx errors propagate immediately.

    `banned_terms` is forwarded to _call_claude for a copy-guard regeneration.
    """
    from anthropic import APIConnectionError, APIStatusError

    try:
        return _call_claude(business, offer, banned_terms)
    except APIConnectionError:
        print(f"  [analyze] connection error, retrying in {RETRY_DELAY}s", file=sys.stderr)
        time.sleep(RETRY_DELAY)
        return _call_claude(business, offer, banned_terms)
    except APIStatusError as exc:
        if exc.status_code == 429 or exc.status_code >= 500:
            print(
                f"  [analyze] transient {exc.status_code}, retrying in {RETRY_DELAY}s",
                file=sys.stderr,
            )
            time.sleep(RETRY_DELAY)
            return _call_claude(business, offer, banned_terms)
        raise


def _normalize(data: dict) -> dict:
    """Coerce the parsed result into the expected shape with safe defaults."""
    pain_points = data.get("pain_points") or []
    if not isinstance(pain_points, list):
        pain_points = [str(pain_points)]

    try:
        score = int(data.get("score", 3))
    except (TypeError, ValueError):
        score = 3
    score = max(1, min(5, score))  # clamp to 1-5

    return {
        "pain_points": [str(p) for p in pain_points],
        "score": score,
        "email_subject": str(data.get("email_subject") or "").strip(),
        "email_body": str(data.get("email_body") or "").strip(),
    }


def analyze_lead(business: dict, offer: dict | None = None) -> dict:
    """Analyze one business against one offer profile and return
    {pain_points, score, email_subject, email_body}.

    `business` should have: name, category, city, website (markdown str or
    None). `offer` is a row dict from tools/lead_db.py's offer_profiles
    table (see get_offer/get_default_offer); if omitted, the workspace's
    default offer is used. Raises RuntimeError if no offer is available at
    all (nothing passed in, and no default offer configured).

    The sender signature (offer sender_name/sender_title, or SENDER_NAME /
    SENDER_PHONE from .env) is appended to every email_body so the column is
    always send-ready.
    """
    if offer is None:
        # None-offer fallback is a direct/CLI convenience — the pipeline
        # resolves the offer per workspace in enrich_leads and passes it in.
        offer = get_default_offer(OWNER_WORKSPACE_ID)
        if offer is None:
            raise RuntimeError(
                "No offer profile available: pass `offer` explicitly or "
                "seed a default row in offer_profiles (tools/lead_db.py)."
            )

    is_person = (business.get("entity_type") or "business") == "person"
    if not business.get("website") and not is_person:
        # No site content to analyze — low-confidence default, no API call.
        # Persons skip this branch on purpose (plan v2/03 C3): with no company
        # website, Claude still gets title+company+industry and scores
        # honestly against that thinner evidence.
        value_prop = (offer.get("value_prop") or "").strip()
        pitch_line = value_prop or "We help local businesses like yours grow."
        result = {
            "pain_points": ["could not scrape website"],
            "score": 2,
            "email_subject": "a couple quick ideas for you",
            "email_body": (
                f"Hi {business.get('name', 'there')} team,\n\n"
                f"{pitch_line} Want a couple of quick ideas tailored to "
                "your business? Happy to send more details.\n\nBest,"
            ),
        }
    else:
        from anthropic import AuthenticationError, PermissionDeniedError

        try:
            result = _normalize(_call_with_retry(business, offer))
            if not result["email_subject"] or len(result["email_body"]) < 40:
                raise ValueError("email copy came back empty or too short")
            # Post-generation copy guard: reject any tool/brand name that leaked
            # into the copy (customers write their own offer text, so enforce on
            # the OUTPUT, not just the prompt). Regenerate once; if still dirty,
            # keep it but flag for review.
            blocked = check_copy(result["email_subject"], result["email_body"])
            if blocked:
                print(
                    f"  [analyze] blocked tool/brand terms in copy: "
                    f"{', '.join(blocked)}; regenerating once",
                    file=sys.stderr,
                )
                result = _normalize(_call_with_retry(business, offer, banned_terms=blocked))
                still_blocked = check_copy(result["email_subject"], result["email_body"])
                if still_blocked:
                    result["copy_flagged"] = True
                    print(
                        f"  [analyze] copy still contains "
                        f"{', '.join(still_blocked)} after regen; flagged for review",
                        file=sys.stderr,
                    )
        except (AuthenticationError, PermissionDeniedError):
            # Misconfigured/revoked API key: every lead would silently become a
            # junk fallback row — abort the whole batch instead.
            raise
        except Exception as exc:  # noqa: BLE001 — degrade gracefully per lead
            print(f"  [analyze] failed: {exc}", file=sys.stderr)
            if is_person:
                # Greet a person by first name, never "team".
                first = (business.get("name") or "there").split()[0]
                greeting = f"Hi {first},"
            else:
                greeting = f"Hi {business.get('name', 'there')} team,"
            result = {
                "pain_points": ["analysis failed"],
                "score": 2,
                "email_subject": "a couple quick automation ideas for you",
                "email_body": (
                    f"{greeting}\n\n"
                    "We help local businesses automate booking, lead capture, and "
                    "customer follow-up. Want a couple of quick ideas tailored to "
                    "your business?\n\nBest,"
                ),
            }

    # Owner copy rule: zero em/en dashes in outreach copy. The prompt asks,
    # this enforces — covers the Claude path and both fallback paths.
    result["email_subject"] = strip_dashes(result["email_subject"]) or ""
    result["email_body"] = strip_dashes(result["email_body"]) or ""

    result["email_body"] += _sender_signature(offer)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze one business lead with Claude.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--website-file", help="path to scraped markdown; omit to simulate a failed scrape")
    args = parser.parse_args()

    website = None
    if args.website_file:
        website = Path(args.website_file).read_text(encoding="utf-8")

    business = {
        "name": args.name,
        "category": args.category,
        "city": args.city,
        "website": website,
    }
    result = analyze_lead(business)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
