"""Tests for the copy guard (tools/copy_guard.py) and its wiring into
tools/analyze_lead.py.

Stdlib unittest only (no pytest, no network, no real API key). The Anthropic
client is faked by monkeypatching tools.analyze_lead._get_client, the
module-level lazy getter, exactly as tests/test_analyze_lead_offer.py does.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import tools.analyze_lead as analyze_lead_module
import tools.copy_guard as copy_guard
from tools.copy_guard import check_copy, find_blocked_terms, strip_dashes


class StripDashesTestCase(unittest.TestCase):
    """Owner copy rule: zero em/en dashes in outreach copy."""

    def test_spaced_em_dash_becomes_comma(self):
        self.assertEqual(strip_dashes("great site — but no booking"),
                         "great site, but no booking")

    def test_unspaced_em_dash_becomes_comma(self):
        self.assertEqual(strip_dashes("fast—simple"), "fast, simple")

    def test_en_dash_number_range_becomes_hyphen(self):
        self.assertEqual(strip_dashes("open 8–17 weekdays"), "open 8-17 weekdays")
        self.assertEqual(strip_dashes("Mon – Fri, 9 – 5"), "Mon, Fri, 9-5")

    def test_dash_next_to_newline_does_not_merge_lines(self):
        self.assertEqual(strip_dashes("line one —\nline two"),
                         "line one,\nline two")

    def test_dash_opening_a_line_leaves_no_orphan_comma(self):
        self.assertEqual(strip_dashes("Perks:\n— fast\n— simple"),
                         "Perks:\nfast\nsimple")

    def test_clean_text_untouched(self):
        text = "Hi Acme team,\n\nWe help local shops book more jobs.\n\nBest,"
        self.assertEqual(strip_dashes(text), text)
        self.assertEqual(strip_dashes("self-serve check-in"), "self-serve check-in")

    def test_none_and_empty_pass_through(self):
        self.assertIsNone(strip_dashes(None))
        self.assertEqual(strip_dashes(""), "")

analyze_lead = analyze_lead_module.analyze_lead


# --- Fakes (sequence-returning, records every create() call) ---
class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.stop_reason = stop_reason
        self.content = [_FakeBlock(json.dumps(payload))]


class _SeqMessages:
    """Fake `client.messages` that returns payloads in order and records the
    kwargs of every create() call. Indexing past the last payload raises, so an
    unexpected extra API call fails the test loudly."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self._payloads[len(self.calls) - 1]  # IndexError if over-called
        return _FakeMessage(payload)


class _SeqClient:
    def __init__(self, payloads):
        self.messages = _SeqMessages(payloads)


OFFER = {
    "id": 1,
    "name": "SMS text-back bot",
    "product_description": "An automated text-back assistant for local businesses.",
    "value_prop": "Catch the calls you're missing and turn them into booked jobs.",
    "target_customer": "Small local service businesses that rely on phone calls.",
    "tone": "casual, direct",
    "cta_style": "one low-pressure soft call-to-action",
    "sender_name": "Jordan Lee",
    "sender_title": "Founder",
    "qualification_hints": "Relies on calls/appointments; no online booking.",
    "disqualifiers": "Already has online booking or live chat.",
}

BUSINESS = {
    "name": "Acme Dental",
    "category": "dentist",
    "city": "Austin TX",
    "website": "# Acme Dental\nWe are open Mon-Fri, call us to book.",
}

_CLEAN_BODY = (
    "Hi Acme Dental team,\n\n"
    "Noticed your site only lists a phone number for booking. We help "
    "businesses like yours capture and follow up on missed calls automatically "
    "so fewer patients slip away.\n\nBest,"
)


def _payload(subject: str, body: str) -> dict:
    return {
        "pain_points": ["no online booking"],
        "score": 4,
        "email_subject": subject,
        "email_body": body,
    }


class FindBlockedTermsTestCase(unittest.TestCase):
    def test_plain_brand_name_hits(self):
        self.assertEqual(find_blocked_terms("book with Calendly today"), ["calendly"])

    def test_trailing_punctuation_still_hits(self):
        self.assertEqual(find_blocked_terms("just use Calendly, it's easy"), ["calendly"])

    def test_dotted_term_hits(self):
        self.assertEqual(find_blocked_terms("head to cal.com to book"), ["cal.com"])

    def test_dotted_term_not_matched_inside_word(self):
        # "cal.com" is a substring of "local.command" but must NOT match — the
        # left boundary sees the word char 'o' before "cal".
        self.assertEqual(find_blocked_terms("run the local.command line tool"), [])

    def test_no_partial_word_match(self):
        self.assertEqual(find_blocked_terms("this is calendlyish, not real"), [])

    def test_case_insensitive(self):
        self.assertEqual(find_blocked_terms("try HUBSPOT or HubSpot"), ["hubspot"])

    def test_multiword_phrase_hits(self):
        self.assertEqual(find_blocked_terms("we run on Jane App here"), ["jane app"])

    def test_multiword_phrase_not_matched_across_word_boundary(self):
        # "jane app" is a prefix of "jane appleseed" but the right boundary sees
        # 'l' after "app" and must not match.
        self.assertEqual(find_blocked_terms("meet jane appleseed today"), [])

    def test_multiple_terms_in_order_found(self):
        text = "we use Zapier then push to HubSpot"
        self.assertEqual(find_blocked_terms(text), ["zapier", "hubspot"])

    def test_each_term_returned_once(self):
        self.assertEqual(find_blocked_terms("Calendly and calendly again"), ["calendly"])

    def test_clean_text_no_hits(self):
        self.assertEqual(find_blocked_terms("just use a booking link, book in a tap"), [])

    def test_check_copy_unions_subject_and_body(self):
        hits = check_copy("try Calendly", "or maybe Zapier")
        self.assertEqual(hits, ["calendly", "zapier"])

    def test_env_extension_picked_up(self):
        # COPY_BLOCKLIST extends the default list (comma-separated, lowercased).
        with patch.object(copy_guard, "COPY_BLOCKLIST", "Notion, Airtable"):
            self.assertEqual(find_blocked_terms("we build on Notion daily"), ["notion"])
            self.assertEqual(find_blocked_terms("stored in Airtable"), ["airtable"])
        # Once unset, the extension is gone again.
        with patch.object(copy_guard, "COPY_BLOCKLIST", None):
            self.assertEqual(find_blocked_terms("we build on Notion daily"), [])


class AnalyzeLeadCopyGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        analyze_lead_module._client = None

    def tearDown(self) -> None:
        analyze_lead_module._client = None

    def test_dirty_then_clean_uses_regen_no_flag_two_calls(self):
        fake = _SeqClient([
            _payload(
                "book via Calendly",
                "Hi Acme Dental team, book your next visit through Calendly and "
                "we will confirm within minutes.\n\nBest,",
            ),
            _payload("a quick idea for your front desk", _CLEAN_BODY),
        ])
        with patch.object(analyze_lead_module, "_get_client", return_value=fake):
            result = analyze_lead(BUSINESS, OFFER)

        self.assertEqual(len(fake.messages.calls), 2)
        self.assertNotIn("copy_flagged", result)
        self.assertIn("capture and follow up", result["email_body"])

    def test_dirty_twice_flags_and_stops_at_two_calls(self):
        fake = _SeqClient([
            _payload(
                "book via Calendly",
                "Hi Acme Dental team, book your next visit through Calendly and "
                "we will confirm within minutes.\n\nBest,",
            ),
            _payload(
                "book via Calendly again",
                "Hi Acme Dental team, you can still reach us through Calendly "
                "whenever you like.\n\nBest,",
            ),
        ])
        with patch.object(analyze_lead_module, "_get_client", return_value=fake):
            result = analyze_lead(BUSINESS, OFFER)

        self.assertEqual(len(fake.messages.calls), 2)  # never a third call
        self.assertTrue(result.get("copy_flagged"))

    def test_clean_first_response_one_call_no_flag(self):
        fake = _SeqClient([_payload("a quick idea for your front desk", _CLEAN_BODY)])
        with patch.object(analyze_lead_module, "_get_client", return_value=fake):
            result = analyze_lead(BUSINESS, OFFER)

        self.assertEqual(len(fake.messages.calls), 1)
        self.assertNotIn("copy_flagged", result)

    def test_regen_prompt_names_the_banned_terms(self):
        fake = _SeqClient([
            _payload(
                "book via Calendly",
                "Hi Acme Dental team, book your next visit through Calendly and "
                "we will confirm within minutes.\n\nBest,",
            ),
            _payload("a quick idea for your front desk", _CLEAN_BODY),
        ])
        with patch.object(analyze_lead_module, "_get_client", return_value=fake):
            analyze_lead(BUSINESS, OFFER)

        regen_prompt = fake.messages.calls[1]["messages"][0]["content"]
        self.assertIn("banned", regen_prompt.lower())
        self.assertIn("calendly", regen_prompt.lower())


if __name__ == "__main__":
    unittest.main()
