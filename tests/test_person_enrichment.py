"""Tests for the person enrichment path (V2 Stage 3, plans/v2/03 C3/E).

All offline, zero paid calls: the Anthropic client, website scraper, widget
detector, and MX check are all faked. Proves:
- analyze_lead builds a person-addressed prompt (and the business prompt is
  untouched);
- a person with no company website still gets a real Claude call with
  title/company/industry evidence (no junk score-2 shortcut);
- enrich_person never mines emails and never runs widget detection;
- process_leads branches per entity_type: widget detect provably skipped for
  persons, --us-only applies to businesses only, contactless persons skip.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import enrich_leads  # noqa: E402  (flat module, as the pipeline runs it)
import analyze_lead as analyze_lead_flat  # noqa: E402  (enrich_leads' import target)

OFFER = {
    "id": 7,
    "name": "SMS text-back bot",
    "product_description": "An automated text-back assistant.",
    "value_prop": "Catch missed calls.",
    "target_customer": "Local service businesses.",
    "tone": "casual",
    "cta_style": "soft ask",
    "sender_name": "Zee",
    "sender_title": None,
    "qualification_hints": None,
    "disqualifiers": None,
}

PERSON_LEAD = {
    "entity_type": "person",
    "name": "Jane Marsh",
    "title": "Founder",
    "company": "Marsh Marketing",
    "category": "marketing agencies",
    "city": "Austin",
    "website": "https://marshmarketing.example",
    "linkedin_url": "https://www.linkedin.com/in/jane-marsh",
    "email": "jane@marshmarketing.example",
    "email_source": "api",
    "source": "b2b_data_api",
    "source_id": "42",
}

BUSINESS_LEAD = {
    "place_id": "gm_1",
    "name": "Bright Smile Dental",
    "category": "dentist",
    "city": "Boise",
    "website": "https://brightsmile.example",
    "phone": "+1 208 555 0100",
}

ANALYSIS = {
    "pain_points": ["no online booking"],
    "score": 4,
    "email_subject": "quick idea for Marsh Marketing",
    "email_body": "Hi Jane, saw your agency and had a thought about missed calls. Best,",
}


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, payload: dict):
        self.stop_reason = "end_turn"
        self.content = [_FakeBlock(json.dumps(payload))]


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


class AnalyzeLeadPersonPromptTestCase(unittest.TestCase):
    """analyze_lead with entity_type='person' (flat module, the one
    enrich_leads actually calls)."""

    def _analyze(self, business: dict) -> tuple[dict, _FakeClient]:
        client = _FakeClient(_FakeMessage(ANALYSIS))
        with mock.patch.object(analyze_lead_flat, "_get_client", return_value=client):
            result = analyze_lead_flat.analyze_lead(business, OFFER)
        return result, client

    def test_person_prompt_addresses_the_person(self):
        person = {
            "entity_type": "person", "name": "Jane Marsh", "title": "Founder",
            "company": "Marsh Marketing", "category": "marketing agencies",
            "city": "Austin", "website": "# Marsh Marketing\nWe do ads.",
        }
        result, client = self._analyze(person)
        kwargs = client.messages.calls[0]
        user_prompt = kwargs["messages"][0]["content"]
        self.assertIn("Person: Jane Marsh", user_prompt)
        self.assertIn("Job title: Founder", user_prompt)
        self.assertIn("Company: Marsh Marketing", user_prompt)
        self.assertIn("Company website content", user_prompt)
        system = kwargs["system"]
        self.assertIn("PERSON", system)
        self.assertIn("first name", system)
        # Business-only wording must be gone on the person path.
        self.assertNotIn("NO individual contact name", system)
        self.assertEqual(result["score"], 4)

    def test_person_without_website_still_gets_a_real_call(self):
        person = {
            "entity_type": "person", "name": "Jane Marsh", "title": "Founder",
            "company": "Marsh Marketing", "category": "marketing agencies",
            "city": "Austin", "website": None,
        }
        result, client = self._analyze(person)
        # One real API call happened — no score-2 no-website shortcut.
        self.assertEqual(len(client.messages.calls), 1)
        user_prompt = client.messages.calls[0]["messages"][0]["content"]
        self.assertIn("No company website content is available", user_prompt)
        self.assertNotIn("could not scrape website", result["pain_points"])

    def test_business_prompt_unchanged(self):
        business = {
            "name": "Bright Smile Dental", "category": "dentist",
            "city": "Boise", "website": "# Bright Smile\nCall us!",
        }
        _, client = self._analyze(business)
        kwargs = client.messages.calls[0]
        self.assertIn("Business name: Bright Smile Dental",
                      kwargs["messages"][0]["content"])
        self.assertIn("NO individual contact name", kwargs["system"])
        self.assertNotIn("PERSON", kwargs["system"])

    def test_business_without_website_keeps_no_call_fallback(self):
        business = {"name": "No Site LLC", "category": "plumber",
                    "city": "Boise", "website": None}
        result, client = self._analyze(business)
        self.assertEqual(client.messages.calls, [])  # no API call
        self.assertEqual(result["score"], 2)


class EnrichPersonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scrape = mock.patch.object(
            enrich_leads, "scrape_website", return_value="# Marsh Marketing"
        )
        self.analyze = mock.patch.object(
            enrich_leads, "analyze_lead", return_value=dict(ANALYSIS)
        )
        self.verify = mock.patch.object(
            enrich_leads, "verify_email", return_value="mx_ok"
        )
        # If either of these ever fires for a person, the test fails loudly.
        self.ensure = mock.patch.object(
            enrich_leads, "ensure_email",
            side_effect=AssertionError("ensure_email must not run for persons"),
        )
        self.detect = mock.patch.object(
            enrich_leads, "detect_existing_solutions",
            side_effect=AssertionError("widget detect must not run for persons"),
        )
        self.fake_scrape = self.scrape.start()
        self.fake_analyze = self.analyze.start()
        self.fake_verify = self.verify.start()
        self.ensure.start()
        self.detect.start()
        for p in (self.scrape, self.analyze, self.verify, self.ensure, self.detect):
            self.addCleanup(p.stop)

    def test_enrich_person_scrapes_company_site_and_keeps_api_email(self):
        result = enrich_leads.enrich_person(dict(PERSON_LEAD), OFFER)
        self.fake_scrape.assert_called_once_with("https://marshmarketing.example")
        sent = self.fake_analyze.call_args[0][0]
        self.assertEqual(sent["entity_type"], "person")
        self.assertEqual(sent["title"], "Founder")
        self.assertEqual(sent["company"], "Marsh Marketing")
        self.assertEqual(sent["website"], "# Marsh Marketing")
        # API email kept, MX-checked, never mined.
        self.assertEqual(result["email"], "jane@marshmarketing.example")
        self.assertEqual(result["email_source"], "api")
        self.assertEqual(result["email_verified"], "mx_ok")
        self.assertEqual(result["offer_id"], 7)
        self.assertEqual(result["linkedin_url"], PERSON_LEAD["linkedin_url"])

    def test_enrich_person_without_site_skips_scrape(self):
        lead = {**PERSON_LEAD, "website": None}
        enrich_leads.enrich_person(lead, OFFER)
        self.fake_scrape.assert_not_called()
        self.assertIsNone(self.fake_analyze.call_args[0][0]["website"])


class ProcessLeadsBranchingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patches = [
            mock.patch.object(enrich_leads, "SLEEP_BETWEEN_LEADS", 0),
            mock.patch.object(enrich_leads, "scrape_website", return_value="# site"),
            mock.patch.object(enrich_leads, "analyze_lead", return_value=dict(ANALYSIS)),
            mock.patch.object(enrich_leads, "verify_email", return_value="mx_ok"),
            mock.patch.object(enrich_leads, "ensure_email",
                              return_value=("hello@brightsmile.example", "mailto")),
        ]
        self.detect = mock.patch.object(
            enrich_leads, "detect_existing_solutions",
            return_value={"has_chat": False, "has_booking": False,
                          "detected": [], "soft_signals": []},
        )
        self.fake_detect = self.detect.start()
        self.addCleanup(self.detect.stop)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_widget_detect_runs_for_business_only(self):
        outcome = enrich_leads.process_leads(
            [dict(BUSINESS_LEAD), dict(PERSON_LEAD)], OFFER
        )
        self.assertEqual(len(outcome["enriched"]), 2)
        # Exactly one detect call, and it was for the business's site.
        self.fake_detect.assert_called_once_with(BUSINESS_LEAD["website"])

    def test_contactless_person_is_skipped(self):
        ghost = {"entity_type": "person", "name": "Ghost"}
        outcome = enrich_leads.process_leads([ghost], OFFER)
        self.assertEqual(outcome["skipped"], 1)
        self.assertEqual(outcome["enriched"], [])

    def test_linkedin_only_person_still_lands(self):
        linked = {"entity_type": "person", "name": "Vis Ible",
                  "linkedin_url": "https://www.linkedin.com/in/vis"}
        outcome = enrich_leads.process_leads([linked], OFFER)
        self.assertEqual(len(outcome["enriched"]), 1)
        self.assertEqual(outcome["skipped"], 0)

    def test_us_only_filter_is_business_only(self):
        foreign_biz = {**BUSINESS_LEAD, "phone": "+44 20 7946 0958"}
        phoneless_person = {**PERSON_LEAD, "phone": None}
        outcome = enrich_leads.process_leads(
            [foreign_biz, phoneless_person], OFFER, us_only=True
        )
        self.assertEqual(outcome["excluded_non_us"], 1)  # the business
        self.assertEqual(len(outcome["enriched"]), 1)    # the person landed
        self.assertEqual(outcome["enriched"][0]["entity_type"], "person")


if __name__ == "__main__":
    unittest.main()
