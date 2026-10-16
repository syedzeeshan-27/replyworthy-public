"""Tests that outreach copy in tools/ is offer-generic, not SMS-bot-specific.

Covers the V2 Stage 1 definition of done: no SMS-bot pitch text remains in any
prompt string under tools/ (the seeded offer in tools/lead_db.py is data and
exempt). Stdlib unittest only (no pytest, no network, no real API key) — the
Anthropic client in classify_reply is faked by monkeypatching its lazy
tools.classify_reply._get_client getter.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.classify_reply as classify_reply_module
import tools.enroll_leads as enroll_leads_module

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"

FAKE_OFFER = {
    "id": 7,
    "name": "Payroll automation suite",
    "product_description": (
        "A payroll automation platform for small accounting firms that "
        "eliminates manual timesheet reconciliation."
    ),
    "value_prop": "Cut payroll processing time from hours to minutes",
    "target_customer": "Small accounting and bookkeeping firms.",
    "tone": "professional, concise",
    "cta_style": "offer a free trial",
    "sender_name": None,
    "sender_title": None,
    "qualification_hints": "Handles payroll manually or with spreadsheets.",
    "disqualifiers": "Already uses a payroll automation vendor.",
}

SMS_PHRASES = ("text-back", "text the caller back", "missed calls")


class _FakeMessages:
    """Fake `client.messages` — records the kwargs of the last create() call."""

    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.content = [_FakeBlock(json.dumps(payload))]


_CLASSIFY_PAYLOAD = {
    "reply_text": "How much does this cost?",
    "our_subject": "a quick idea for your front desk",
    "our_body": "Hi Acme Dental team, ...",
    "name": "Acme Dental",
    "category": "dentist",
    "city": "Austin TX",
}

_CLASSIFY_RESULT = {
    "classification": "interested",
    "confidence": "high",
    "summary": "asks price",
    "suggested_action": "reply with pricing",
}


class ClassifyReplyOfferTestCase(unittest.TestCase):
    def setUp(self) -> None:
        classify_reply_module._client = None

    def tearDown(self) -> None:
        classify_reply_module._client = None

    def test_prompt_names_offer_when_passed(self):
        fake_client = _FakeClient(_FakeMessage(_CLASSIFY_RESULT))
        with patch.object(classify_reply_module, "_get_client", return_value=fake_client):
            result = classify_reply_module.classify_reply(_CLASSIFY_PAYLOAD, FAKE_OFFER)

        system_prompt = fake_client.messages.last_kwargs["system"]
        self.assertIn(FAKE_OFFER["name"], system_prompt)
        self.assertIn(FAKE_OFFER["product_description"], system_prompt)
        for phrase in SMS_PHRASES:
            self.assertNotIn(phrase, system_prompt.lower())
        self.assertEqual(result["classification"], "interested")

    def test_prompt_is_neutral_without_offer(self):
        fake_client = _FakeClient(_FakeMessage(_CLASSIFY_RESULT))
        with patch.object(classify_reply_module, "_get_client", return_value=fake_client):
            result = classify_reply_module.classify_reply(_CLASSIFY_PAYLOAD)

        system_prompt = fake_client.messages.last_kwargs["system"]
        self.assertIn("a product or service to local businesses", system_prompt)
        for phrase in SMS_PHRASES:
            self.assertNotIn(phrase, system_prompt.lower())
        self.assertEqual(result["classification"], "interested")


class EnrollLeadsOfferCopyTestCase(unittest.TestCase):
    def test_follow_ups_render_offer_value_prop(self):
        steps = enroll_leads_module.build_default_steps(FAKE_OFFER)

        # Step numbers, day offsets, and the step-1 "no template" contract
        # are load-bearing for the sequencer — assert them unchanged.
        self.assertEqual([(n, d) for n, d, _ in steps], [(1, 0), (2, 3), (3, 4)])
        self.assertIsNone(steps[0][2])

        for _, _, body in steps[1:]:
            self.assertIn(FAKE_OFFER["value_prop"], body)
            self.assertIn("{name}", body)  # send-time merge placeholder survives
            for phrase in SMS_PHRASES:
                self.assertNotIn(phrase, body.lower())


class NoSmsBotTextInToolsTestCase(unittest.TestCase):
    """Grep-style definition-of-done check: the SMS-bot pitch phrases live
    only in tools/lead_db.py (the seeded offer row — data, not a prompt) and
    tools/seed_demo.py (made-up sample data for the demo)."""

    ALLOWED = {"lead_db.py", "seed_demo.py"}
    PHRASES = ("text-back", "missed calls")

    def test_no_sms_bot_phrases_outside_lead_db(self):
        offenders = []
        for path in sorted(TOOLS_DIR.glob("*.py")):
            if path.name in self.ALLOWED:
                continue
            content = path.read_text(encoding="utf-8").lower()
            for phrase in self.PHRASES:
                if phrase in content:
                    offenders.append(f"{path.name}: {phrase!r}")
        self.assertEqual(offenders, [], f"SMS-bot text found in tools/: {offenders}")


if __name__ == "__main__":
    unittest.main()
