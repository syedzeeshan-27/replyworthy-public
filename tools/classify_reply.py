"""Classify a prospect's reply with Claude — structured outputs, fail-loud auth.

Stage-3 classification (plans/03_STAGE3_REPLY_HANDLING.md B). Reuses the exact
API pattern proven in tools/analyze_lead.py: model from settings.LEADGEN_MODEL
(never hardcoded), thinking disabled, low effort, output_config json_schema
(no hand-rolled JSON parsing), one app-level retry on connection/429/5xx, and
AuthenticationError/PermissionDeniedError re-raised on purpose — a bad key
must halt the batch, not silently mis-classify a day of replies.

Bounces never reach this module (tools/read_inbox.py classifies them
mechanically), so the enum here is human-reply classes only.

Usage (CLI, one reply):
    python tools/classify_reply.py --reply "How much does this cost?" \
        --our-subject "a quick idea for your front desk" --name "Bright Smile Dental"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import LEADGEN_MODEL, require  # noqa: E402

MODEL = LEADGEN_MODEL
MAX_TOKENS = 1024
MAX_REPLY_CHARS = 4000
RETRY_DELAY = 20

def _build_system_prompt(offer: dict | None) -> str:
    """Build the classification system prompt.

    When an offer profile (tools/lead_db.py offer_profiles row) is provided,
    the prompt names what the agency actually sells; otherwise it stays
    offer-neutral. The classification labels and rules are fixed either way.
    """
    if offer is not None:
        selling = (
            f"selling {offer.get('name')}: {offer.get('product_description')}"
        )
    else:
        selling = "selling a product or service to local businesses"
    return (
        "You classify replies to cold outreach emails for a small agency "
        f"{selling}. Given the email we sent "
        "and the prospect's reply, produce:\n"
        '  - "classification": exactly one of\n'
        '      interested       - wants to know more: any question about price, how it '
        "works, availability, a demo/video, or an explicit yes.\n"
        '      not_interested   - a clear, calm no ("not for us", "we\'re good, thanks").\n'
        '      unsubscribe      - asks to stop being contacted ("take me off your '
        'list", "stop emailing me", "no thanks" as opt-out). Legal difference from '
        "not_interested: unsubscribe means never contact again.\n"
        '      out_of_office    - automatic away/vacation reply.\n'
        '      wrong_person     - says they are not the right contact or the address '
        "reached someone else.\n"
        '      needs_human      - anything a human must read: anger, complaints, '
        "legal threats, confusion about who we are, mixed signals, negotiation, or "
        "anything that fits no other class cleanly.\n"
        '  - "confidence": high | medium | low. Be honest - when in doubt, lower it. '
        "Anger or legal language is ALWAYS needs_human, never not_interested.\n"
        '  - "summary": one short line a busy owner reads on their phone '
        '(e.g. "asks price, wants demo video").\n'
        '  - "suggested_action": one short imperative line for the human '
        '(e.g. "reply with pricing + 2-min video link").\n'
        "Classify only from the reply text. Never invent details."
    )


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [
                "interested", "not_interested", "unsubscribe",
                "out_of_office", "wrong_person", "needs_human",
            ],
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary": {"type": "string"},
        "suggested_action": {"type": "string"},
    },
    "required": ["classification", "confidence", "summary", "suggested_action"],
    "additionalProperties": False,
}


_client = None


def _get_client():
    """Lazily construct (and cache) the Anthropic client.

    A module-level getter (rather than a plain module-level client) so tests
    can monkeypatch `tools.classify_reply._get_client` with a fake object
    without needing a real ANTHROPIC_API_KEY or making a network call.
    """
    global _client
    if _client is None:
        from anthropic import Anthropic  # lazy import: only needed for live calls

        _client = Anthropic(api_key=require("ANTHROPIC_API_KEY"))
    return _client


def _call_claude(payload: dict, offer: dict | None) -> dict:
    client = _get_client()
    user_prompt = (
        f"Business: {payload.get('name')} ({payload.get('category')}, {payload.get('city')})\n\n"
        f"Email we sent:\nSubject: {payload.get('our_subject')}\n{payload.get('our_body') or '(body unavailable)'}\n\n"
        f"Their reply:\n{(payload.get('reply_text') or '')[:MAX_REPLY_CHARS]}"
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Short classification task - thinking tokens are waste.
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=_build_system_prompt(offer),
        messages=[{"role": "user", "content": user_prompt}],
    )
    if msg.stop_reason == "max_tokens":
        raise ValueError("response truncated at max_tokens")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def _call_with_retry(payload: dict, offer: dict | None) -> dict:
    from anthropic import APIConnectionError, APIStatusError

    try:
        return _call_claude(payload, offer)
    except APIConnectionError:
        print(f"  [classify] connection error, retrying in {RETRY_DELAY}s", file=sys.stderr)
        time.sleep(RETRY_DELAY)
        return _call_claude(payload, offer)
    except APIStatusError as exc:
        if exc.status_code == 429 or exc.status_code >= 500:
            print(f"  [classify] transient {exc.status_code}, retrying in {RETRY_DELAY}s",
                  file=sys.stderr)
            time.sleep(RETRY_DELAY)
            return _call_claude(payload, offer)
        raise


def classify_reply(payload: dict, offer: dict | None = None) -> dict:
    """Classify one reply. `payload` keys: reply_text, our_subject, our_body,
    name, category, city. `offer` is an optional offer_profiles row dict
    (tools/lead_db.py) naming what the outreach was selling; without it the
    prompt uses offer-neutral wording. Returns {classification, confidence,
    summary, suggested_action}. Auth errors propagate (halt the batch); any
    other failure degrades to needs_human/low so a human still sees the reply."""
    from anthropic import AuthenticationError, PermissionDeniedError

    try:
        result = _call_with_retry(payload, offer)
        return {
            "classification": result["classification"],
            "confidence": result["confidence"],
            "summary": str(result["summary"]).strip()[:200],
            "suggested_action": str(result["suggested_action"]).strip()[:200],
        }
    except (AuthenticationError, PermissionDeniedError):
        raise
    except Exception as exc:  # noqa: BLE001 - degrade per reply, human catches it
        print(f"  [classify] failed: {exc}", file=sys.stderr)
        return {
            "classification": "needs_human",
            "confidence": "low",
            "summary": "automatic classification failed - read manually",
            "suggested_action": "open the reply and handle it yourself",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify one outreach reply with Claude.")
    parser.add_argument("--reply", required=True)
    parser.add_argument("--our-subject", default="")
    parser.add_argument("--our-body", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--city", default="")
    args = parser.parse_args()

    result = classify_reply({
        "reply_text": args.reply, "our_subject": args.our_subject,
        "our_body": args.our_body, "name": args.name,
        "category": args.category, "city": args.city,
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
