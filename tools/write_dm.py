"""Write ONE short personalized social DM for a lead (social links + DM queue).

The DM queue's generation step: leads with no email but a Facebook/Instagram
page get a Claude-written DM that a HUMAN copies, pastes, and sends from the
platform — the system never automates any social network. Copies
analyze_lead.py's platform call shape (model from LEADGEN_MODEL, structured
outputs, auth-error re-raise, copy_guard on the OUTPUT, zero em dashes).

Two deliberate differences from batch enrichment:
- No junk fallback copy. One click generates one message a person will paste;
  an honest error beats filler, so failures raise.
- Wording must VARY between messages (owner practice: repeated identical
  openers get an account flagged as spam). The prompt demands fresh phrasing
  and callers pass `avoid_openings` — opening lines of recently sent DMs.

Usage (CLI, for testing one lead):
    python tools/write_dm.py --name "Tampa Smiles" --category dentist \
        --city "Tampa, FL"
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
from tools.copy_guard import find_blocked_terms, strip_dashes  # noqa: E402
from tools.lead_db import OWNER_WORKSPACE_ID, get_default_offer  # noqa: E402

MODEL = LEADGEN_MODEL
MAX_TOKENS = 512  # one short DM, never an essay
RETRY_DELAY = 20  # seconds before the single app-level retry on 429/5xx
MIN_DM_CHARS = 25  # anything shorter is a failed generation, not a message

# The API guarantees valid JSON matching this shape (same structured-output
# rule as analyze_lead — no fence-stripping, no brace-hunting).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"dm_text": {"type": "string"}},
    "required": ["dm_text"],
    "additionalProperties": False,
}

# Fixed mechanics for every DM, regardless of offer. Pitch content comes from
# the offer profile below.
_DM_RULES = (
    "Write ONE short direct message (roughly 40-80 words) to be sent from a "
    "personal account to this business's Facebook or Instagram page. Casual, "
    "human, easy to read on a phone. Reference ONE concrete detail about this "
    "specific business so it can't read as a blast. Vary your opening and "
    "closing phrasing — never a stock template line, because repeated "
    "identical openers get the sender's account flagged as spam. Do not name "
    "a specific product, tool, brand, or URL — describe what it does in plain "
    'language instead (e.g. "a booking link"). Do NOT include links, prices, '
    "guarantees, or invented details. Never use em dashes (—) or en dashes "
    "(–); use a comma, a period, or the word 'to' instead. End with one "
    "low-pressure question."
)


def _build_system_prompt(offer: dict) -> str:
    product_description = (offer.get("product_description") or "").strip()
    value_prop = (offer.get("value_prop") or "").strip()
    tone = (offer.get("tone") or "casual, friendly").strip()
    sender_name = (offer.get("sender_name") or "").strip()

    prompt = (
        "You write first-touch social DMs for an agency that sells the "
        f"following offer to small local businesses: {product_description} "
        f"Value proposition: {value_prop} "
        f"Write in this tone: {tone}. {_DM_RULES}"
    )
    if sender_name:
        first = sender_name.split()[0]
        prompt += f" Sign off casually with just the first name {first}."
    return (
        prompt
        + ' Produce one field: "dm_text" — the complete message, ready to paste.'
    )


_client = None


def _make_client():
    """The real (cached) Anthropic client. Tests monkeypatch `_get_client`
    with a fake and restore it to this — same pattern as analyze_lead."""
    global _client
    if _client is None:
        from anthropic import Anthropic  # lazy import: only needed for live calls

        _client = Anthropic(api_key=require("ANTHROPIC_API_KEY"))
    return _client


_get_client = _make_client


def _user_prompt(lead: dict, avoid_openings: list[str] | None,
                 banned_terms: list[str] | None) -> str:
    prompt = (
        f"Business name: {lead.get('name')}\n"
        f"Category: {lead.get('category') or '(unknown)'}\n"
        f"City: {lead.get('city') or '(unknown)'}\n"
    )
    if lead.get("area"):
        prompt += f"Area: {lead['area']}\n"
    pain_points = lead.get("pain_points") or []
    if isinstance(pain_points, str):
        try:
            pain_points = json.loads(pain_points)  # DB rows store JSON text
        except ValueError:
            pain_points = [pain_points]
    if pain_points:
        prompt += "Known gaps to speak to: " + "; ".join(
            str(p) for p in pain_points) + "\n"
    if avoid_openings:
        prompt += (
            "\nRecently sent DMs opened with the lines below. Yours must "
            "open DIFFERENTLY from all of them:\n"
            + "\n".join(f"- {o}" for o in avoid_openings)
            + "\n"
        )
    if banned_terms:
        prompt += (
            "\nThe previous draft used these banned tool/brand names: "
            f"{', '.join(banned_terms)}. Do NOT use these or any other "
            "product, tool, brand, or company name. Describe the capability "
            "generically instead."
        )
    return prompt


def _call_claude(lead: dict, offer: dict, avoid_openings: list[str] | None,
                 banned_terms: list[str] | None = None) -> str:
    client = _get_client()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Same rule as analyze_lead: short copy task, thinking tokens are waste.
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=_build_system_prompt(offer),
        messages=[{"role": "user",
                   "content": _user_prompt(lead, avoid_openings, banned_terms)}],
    )
    if msg.stop_reason == "max_tokens":
        raise ValueError("DM truncated at max_tokens")
    text = next(b.text for b in msg.content if b.type == "text")
    dm_text = str(json.loads(text).get("dm_text") or "").strip()
    if len(dm_text) < MIN_DM_CHARS:
        raise ValueError("DM came back empty or too short")
    return dm_text


def _call_with_retry(lead: dict, offer: dict, avoid_openings: list[str] | None,
                     banned_terms: list[str] | None = None) -> str:
    """One app-level retry on rate-limit/5xx/network errors. Auth and other
    4xx errors propagate immediately (never silently degrade)."""
    from anthropic import APIConnectionError, APIStatusError

    try:
        return _call_claude(lead, offer, avoid_openings, banned_terms)
    except APIConnectionError:
        print(f"  [write_dm] connection error, retrying in {RETRY_DELAY}s",
              file=sys.stderr)
        time.sleep(RETRY_DELAY)
        return _call_claude(lead, offer, avoid_openings, banned_terms)
    except APIStatusError as exc:
        if exc.status_code == 429 or exc.status_code >= 500:
            print(f"  [write_dm] transient {exc.status_code}, retrying in "
                  f"{RETRY_DELAY}s", file=sys.stderr)
            time.sleep(RETRY_DELAY)
            return _call_claude(lead, offer, avoid_openings, banned_terms)
        raise


def write_dm(lead: dict, offer: dict | None = None,
             avoid_openings: list[str] | None = None) -> dict:
    """One ready-to-paste DM for `lead` against `offer`.

    Returns {"dm_text": str} plus "copy_flagged": True when a tool/brand name
    survived the one copy-guard regeneration (keep it visible for review, the
    human decides). Raises on any failure — auth errors per the platform
    rule, and everything else because a failed generation must never hand the
    human filler text to paste.
    """
    if offer is None:
        offer = get_default_offer(OWNER_WORKSPACE_ID)
        if offer is None:
            raise RuntimeError(
                "No offer profile available: pass `offer` explicitly or "
                "seed a default row in offer_profiles (tools/lead_db.py)."
            )

    dm_text = _call_with_retry(lead, offer, avoid_openings)
    result: dict = {}

    # Post-generation copy guard, on the OUTPUT (customers write their own
    # offer text): regenerate once naming the leaked terms; if still dirty,
    # keep it but flag for review.
    blocked = find_blocked_terms(dm_text)
    if blocked:
        print(f"  [write_dm] blocked tool/brand terms in DM: "
              f"{', '.join(blocked)}; regenerating once", file=sys.stderr)
        dm_text = _call_with_retry(lead, offer, avoid_openings,
                                   banned_terms=blocked)
        still_blocked = find_blocked_terms(dm_text)
        if still_blocked:
            result["copy_flagged"] = True
            print(f"  [write_dm] DM still contains "
                  f"{', '.join(still_blocked)} after regen; flagged for review",
                  file=sys.stderr)

    # Owner copy rule: zero em/en dashes, enforced in code on every path.
    result["dm_text"] = strip_dashes(dm_text) or ""
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Write one social DM for a lead.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--city", required=True)
    args = parser.parse_args()

    result = write_dm({"name": args.name, "category": args.category,
                       "city": args.city})
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
