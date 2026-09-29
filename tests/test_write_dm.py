"""Tests for the DM writer (tools/write_dm.py — social links + DM queue).

One short, varied, personalized social DM per call, for leads reachable only
through Facebook/Instagram. Copies analyze_lead's platform call shape:
LEADGEN_MODEL, structured outputs, auth-error re-raise, copy_guard on the
OUTPUT, zero em dashes. Unlike batch enrichment there is NO junk fallback —
a failed generation raises so the human never pastes filler.

Stdlib unittest only; the Anthropic client is faked by monkeypatching
tools.write_dm._get_client. No network, no keys.

Run (from the repo root):
    python -m unittest tests.test_write_dm -v
"""
from __future__ import annotations

import json
import unittest

import httpx
from anthropic import AuthenticationError

from config.settings import LEADGEN_MODEL
import tools.write_dm as write_dm_module

write_dm = write_dm_module.write_dm

OFFER = {
    "id": 1,
    "name": "SMS text-back bot",
    "product_description": (
        "An automated text-back assistant: when a customer calls and isn't "
        "answered, it texts them back in seconds."
    ),
    "value_prop": "Catch the calls you're missing and turn them into booked jobs.",
    "tone": "casual, direct, value-first",
    "cta_style": "one low-pressure soft call-to-action",
    "sender_name": "Sam Field",
    "sender_title": None,
}

LEAD = {
    "name": "Tampa Smiles",
    "category": "dentist",
    "city": "Tampa, FL",
    "pain_points": ["no online booking", "phone-only contact"],
    "social_links": {"facebook": "https://facebook.com/tampasmiles"},
}


class _FakeMessages:
    """Fake `client.messages` — returns queued responses in order, recording
    every create() call's kwargs."""

    def __init__(self, responses=None, exception=None):
        self._responses = list(responses or [])
        self._exception = exception
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exception is not None:
            raise self._exception
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses=None, exception=None):
        self.messages = _FakeMessages(responses=responses, exception=exception)


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.content = [_FakeBlock(json.dumps(payload))]


def _dm_response(text: str) -> _FakeMessage:
    return _FakeMessage({"dm_text": text})


def _auth_error() -> AuthenticationError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(401, request=req)
    return AuthenticationError("invalid x-api-key", response=resp, body=None)


GOOD_DM = ("Hey Tampa Smiles team, noticed folks can only reach you by phone. "
           "We text missed callers back in seconds so they book instead of "
           "calling the next office. Worth a quick look?")


class WriteDmTestCase(unittest.TestCase):
    def setUp(self) -> None:
        write_dm_module._client = None

    def tearDown(self) -> None:
        write_dm_module._client = None

    def _run(self, responses, lead=None, **kwargs):
        fake = _FakeClient(responses=responses)
        write_dm_module._get_client = lambda: fake
        try:
            result = write_dm(dict(lead or LEAD), OFFER, **kwargs)
        finally:
            write_dm_module._get_client = write_dm_module._make_client
        return result, fake

    def test_returns_the_generated_dm_personalized_to_the_lead(self):
        result, fake = self._run([_dm_response(GOOD_DM)])
        self.assertEqual(result["dm_text"], GOOD_DM)
        prompt = fake.messages.calls[0]["messages"][0]["content"]
        self.assertIn("Tampa Smiles", prompt)
        self.assertIn("no online booking", prompt)

    def test_call_shape_matches_the_platform_rules(self):
        _result, fake = self._run([_dm_response(GOOD_DM)])
        kwargs = fake.messages.calls[0]
        self.assertEqual(kwargs["model"], LEADGEN_MODEL)
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["thinking"], {"type": "disabled"})

    def test_auth_errors_re_raise_never_degrade(self):
        fake = _FakeClient(exception=_auth_error())
        write_dm_module._get_client = lambda: fake
        try:
            with self.assertRaises(AuthenticationError):
                write_dm(dict(LEAD), OFFER)
        finally:
            write_dm_module._get_client = write_dm_module._make_client

    def test_failures_raise_instead_of_returning_filler(self):
        """One click generates one DM a human will paste — junk fallback copy
        (the batch-enrich behavior) would be worse than an honest error."""
        fake = _FakeClient(responses=[_FakeMessage({"dm_text": ""})])
        write_dm_module._get_client = lambda: fake
        try:
            with self.assertRaises(ValueError):
                write_dm(dict(LEAD), OFFER)
        finally:
            write_dm_module._get_client = write_dm_module._make_client

    def test_blocked_tool_names_trigger_one_regeneration(self):
        dirty = "Hey team, we set you up on Calendly so missed calls book themselves."
        result, fake = self._run([_dm_response(dirty), _dm_response(GOOD_DM)])
        self.assertEqual(result["dm_text"], GOOD_DM)
        self.assertNotIn("copy_flagged", result)
        self.assertEqual(len(fake.messages.calls), 2)
        self.assertIn("calendly", fake.messages.calls[1]["messages"][0]["content"].lower())

    def test_still_dirty_after_regen_is_flagged_for_review(self):
        dirty = "We plug Calendly into your page so bookings run themselves, interested?"
        result, _fake = self._run([_dm_response(dirty), _dm_response(dirty)])
        self.assertTrue(result["copy_flagged"])

    def test_em_dashes_never_survive(self):
        result, _fake = self._run([_dm_response(
            "Hey Tampa Smiles team — noticed the phone-only contact — we text "
            "missed callers back so they book with you. Worth a look?")])
        self.assertNotIn("—", result["dm_text"])
        self.assertNotIn("–", result["dm_text"])

    def test_avoid_openings_reach_the_prompt(self):
        _result, fake = self._run(
            [_dm_response(GOOD_DM)],
            avoid_openings=["Hey Tampa Smiles team, quick one for you."])
        prompt = fake.messages.calls[0]["messages"][0]["content"]
        self.assertIn("quick one for you", prompt)


if __name__ == "__main__":
    unittest.main()
