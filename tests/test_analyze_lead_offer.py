"""Tests for the offer-driven analyze_lead() in tools/analyze_lead.py.

Stdlib unittest only (no pytest, no network, no real API key). The Anthropic
client is faked by monkeypatching tools.analyze_lead._get_client, the
module-level lazy getter, so no real ANTHROPIC_API_KEY is ever needed.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx
from anthropic import AuthenticationError

import tools.analyze_lead as analyze_lead_module

analyze_lead = analyze_lead_module.analyze_lead


SMS_OFFER = {
    "id": 1,
    "name": "SMS text-back bot",
    "product_description": (
        "An automated text-back assistant for small local businesses: when a "
        "customer calls and isn't answered, it texts them back in seconds."
    ),
    "value_prop": "Catch the calls you're missing and turn them into booked jobs.",
    "target_customer": "Small local service businesses that rely on phone calls.",
    "tone": "casual, direct, value-first",
    "cta_style": "one low-pressure soft call-to-action",
    "sender_name": None,
    "sender_title": None,
    "qualification_hints": "Relies on calls/appointments; no online booking.",
    "disqualifiers": "Already has online booking or live chat.",
}

OTHER_OFFER = {
    "id": 2,
    "name": "Payroll automation suite",
    "product_description": (
        "A payroll automation platform for small accounting firms that "
        "eliminates manual timesheet reconciliation."
    ),
    "value_prop": "Cut payroll processing time from hours to minutes.",
    "target_customer": "Small accounting and bookkeeping firms.",
    "tone": "professional, concise",
    "cta_style": "offer a free trial",
    "sender_name": "Jordan Lee",
    "sender_title": "Founder",
    "qualification_hints": "Handles payroll manually or with spreadsheets.",
    "disqualifiers": "Already uses a payroll automation vendor.",
}


class _FakeMessages:
    """Fake `client.messages` — records the kwargs of the last create() call."""

    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._exception is not None:
            raise self._exception
        return self._response


class _FakeClient:
    def __init__(self, response=None, exception=None):
        self.messages = _FakeMessages(response=response, exception=exception)


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.content = [_FakeBlock(json.dumps(payload))]


_GOOD_PAYLOAD = {
    "pain_points": ["no online booking", "phone-only contact"],
    "score": 4,
    "email_subject": "a quick idea for your front desk",
    "email_body": (
        "Hi Acme Dental team,\n\n"
        "Noticed your site doesn't have a way for people to reach you outside "
        "office hours. We help businesses like yours capture and follow up on "
        "those missed opportunities automatically.\n\nBest,"
    ),
}

_BUSINESS_WITH_SITE = {
    "name": "Acme Dental",
    "category": "dentist",
    "city": "Austin TX",
    "website": "# Acme Dental\nWe are open Mon-Fri, call us to book.",
}


def _auth_error() -> AuthenticationError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(401, request=req)
    return AuthenticationError("invalid x-api-key", response=resp, body=None)


class AnalyzeLeadOfferTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the cached client between tests so one test's fake doesn't
        # leak into the next via the module-level cache.
        analyze_lead_module._client = None

    def tearDown(self) -> None:
        analyze_lead_module._client = None

    def test_em_en_dashes_stripped_from_generated_copy(self):
        dashed = {
            **_GOOD_PAYLOAD,
            "email_subject": "quick idea — for your front desk",
            "email_body": (
                "Hi Acme Dental team,\n\n"
                "Your site looks great — but you're only reachable 8–17. We "
                "help you catch the rest — automatically, no extra staff.\n\n"
                "Best,"
            ),
        }
        fake_client = _FakeClient(response=_FakeMessage(dashed))
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client):
            result = analyze_lead(_BUSINESS_WITH_SITE, OTHER_OFFER)

        for dash in ("—", "–"):
            self.assertNotIn(dash, result["email_subject"])
            self.assertNotIn(dash, result["email_body"])
        self.assertIn("8-17", result["email_body"])  # range kept, hyphenated
        # The prompt also forbids them up front (belt and braces).
        self.assertIn("em dashes", fake_client.messages.last_kwargs["system"])

    def test_offer_text_in_system_prompt_and_sms_phrases_absent_for_other_offer(self):
        fake_client = _FakeClient(response=_FakeMessage(_GOOD_PAYLOAD))
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client):
            result = analyze_lead(_BUSINESS_WITH_SITE, OTHER_OFFER)

        system_prompt = fake_client.messages.last_kwargs["system"]
        self.assertIn(OTHER_OFFER["product_description"], system_prompt)
        self.assertIn(OTHER_OFFER["qualification_hints"], system_prompt)
        self.assertIn(OTHER_OFFER["target_customer"], system_prompt)

        for phrase in ("text-back", "texts them back", "missed calls"):
            self.assertNotIn(phrase.lower(), system_prompt.lower())

        self.assertEqual(result["score"], 4)

    def test_sms_offer_text_appears_when_sms_offer_passed(self):
        fake_client = _FakeClient(response=_FakeMessage(_GOOD_PAYLOAD))
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client):
            analyze_lead(_BUSINESS_WITH_SITE, SMS_OFFER)

        system_prompt = fake_client.messages.last_kwargs["system"]
        self.assertIn(SMS_OFFER["product_description"], system_prompt)
        self.assertIn(SMS_OFFER["qualification_hints"], system_prompt)

    def test_offer_none_uses_default_offer(self):
        fake_client = _FakeClient(response=_FakeMessage(_GOOD_PAYLOAD))
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client), \
             patch.object(analyze_lead_module, "get_default_offer", return_value=SMS_OFFER) as mock_default, \
             patch.object(analyze_lead_module, "require", return_value="unused"):
            result = analyze_lead(_BUSINESS_WITH_SITE)

        mock_default.assert_called_once()
        self.assertEqual(result["score"], 4)

    def test_offer_none_and_no_default_raises_runtime_error(self):
        with patch.object(analyze_lead_module, "get_default_offer", return_value=None):
            with self.assertRaises(RuntimeError):
                analyze_lead(_BUSINESS_WITH_SITE)

    def test_authentication_error_propagates(self):
        fake_client = _FakeClient(exception=_auth_error())
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client):
            with self.assertRaises(AuthenticationError):
                analyze_lead(_BUSINESS_WITH_SITE, OTHER_OFFER)

    def test_generic_exception_falls_back_to_score_2(self):
        fake_client = _FakeClient(exception=ValueError("boom"))
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client):
            result = analyze_lead(_BUSINESS_WITH_SITE, OTHER_OFFER)

        self.assertEqual(result["score"], 2)
        self.assertEqual(result["pain_points"], ["analysis failed"])
        self.assertTrue(result["email_body"])

    def test_sender_name_offer_uses_offer_signature(self):
        fake_client = _FakeClient(response=_FakeMessage(_GOOD_PAYLOAD))
        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client):
            result = analyze_lead(_BUSINESS_WITH_SITE, OTHER_OFFER)

        self.assertIn("Jordan Lee", result["email_body"])
        self.assertIn("Founder", result["email_body"])

    def test_offer_without_sender_name_uses_settings_fallback(self):
        fake_client = _FakeClient(response=_FakeMessage(_GOOD_PAYLOAD))

        def fake_require(key: str) -> str:
            return {"SENDER_NAME": "Fallback Sender", "SENDER_PHONE": "555-0100"}[key]

        with patch.object(analyze_lead_module, "_get_client", return_value=fake_client), \
             patch.object(analyze_lead_module, "require", side_effect=fake_require):
            result = analyze_lead(_BUSINESS_WITH_SITE, SMS_OFFER)

        self.assertIn("Fallback Sender", result["email_body"])
        self.assertIn("555-0100", result["email_body"])

    def test_output_schema_unchanged(self):
        expected = {
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
        self.assertEqual(analyze_lead_module.OUTPUT_SCHEMA, expected)


if __name__ == "__main__":
    unittest.main()
