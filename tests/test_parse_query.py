"""Parser + router tests for tools/parse_query.py (V2 Stage 2, plan v2/02 E).

All offline: the Anthropic client is faked by monkeypatching
parse_query._get_client, exactly like the analyze_lead tests. Zero paid calls.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import parse_query  # noqa: E402


def _fake_client(payload: dict):
    """A stand-in Anthropic client whose messages.create returns `payload`
    as the structured-output text block."""
    msg = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )
    return SimpleNamespace(
        messages=SimpleNamespace(create=mock.Mock(return_value=msg))
    )


# What the model would return for a clean business query.
BUSINESS_PAYLOAD = {
    "entity_type": "business",
    "source": "google_maps",
    "industry": "dentists",
    "keywords": ["dentist"],
    "geo": {"city": "Boise", "region": "ID", "country": ""},
    "role": "",
    "seniority": "",
    "count": 20,
    "qualification_hints": "without online booking",
    "unsupported": [],
    "confidence": "high",
}

PERSON_PAYLOAD = {
    "entity_type": "person",
    "source": "b2b_data_api",
    "industry": "marketing agencies",
    "keywords": ["marketing agency"],
    "geo": {"city": "Austin", "region": "", "country": ""},
    "role": "founder",
    "seniority": "owner",
    "count": 100,
    "qualification_hints": "",
    "unsupported": [],
    "confidence": "high",
}


def _parse(payload: dict, query: str = "some query") -> dict:
    with mock.patch.object(parse_query, "_get_client",
                           return_value=_fake_client(payload)):
        return parse_query.parse_query(query)


class ParseQueryTestCase(unittest.TestCase):
    def test_clean_business_query(self):
        spec = _parse(BUSINESS_PAYLOAD, "dentists in Boise without online booking, 20 of them")
        self.assertEqual(spec["entity_type"], "business")
        self.assertEqual(spec["source"], "google_maps")
        self.assertEqual(spec["industry"], "dentists")
        self.assertEqual(spec["geo"], {"city": "Boise", "region": "ID", "country": None})
        self.assertEqual(spec["count"], 20)
        self.assertEqual(spec["qualification_hints"], "without online booking")
        self.assertEqual(spec["confidence"], "high")
        self.assertEqual(spec["raw_query"],
                         "dentists in Boise without online booking, 20 of them")
        self.assertNotIn("requested_count", spec)

    def test_clean_person_query(self):
        spec = _parse(PERSON_PAYLOAD, "founders of marketing agencies in Austin, ~100")
        self.assertEqual(spec["entity_type"], "person")
        self.assertEqual(spec["source"], "b2b_data_api")
        self.assertEqual(spec["role"], "founder")
        self.assertEqual(spec["count"], 100)

    def test_router_overrides_model_source(self):
        # The model proposes, the router disposes: a business spec routed to
        # the person connector (or vice versa) by the model gets corrected.
        wrong_business = {**BUSINESS_PAYLOAD, "source": "b2b_data_api"}
        self.assertEqual(_parse(wrong_business)["source"], "google_maps")
        wrong_person = {**PERSON_PAYLOAD, "source": "google_maps"}
        self.assertEqual(_parse(wrong_person)["source"], "b2b_data_api")

    def test_unsupported_ask_preserved(self):
        payload = {**BUSINESS_PAYLOAD,
                   "unsupported": ["Instagram influencers", "their follower counts"]}
        spec = _parse(payload, "scrape Instagram influencers")
        self.assertEqual(spec["unsupported"],
                         ["Instagram influencers", "their follower counts"])

    def test_ambiguous_query_keeps_low_confidence(self):
        payload = {**BUSINESS_PAYLOAD, "confidence": "low",
                   "geo": {"city": "", "region": "", "country": ""}}
        spec = _parse(payload, "dentists")
        self.assertEqual(spec["confidence"], "low")
        self.assertEqual(spec["geo"], {"city": None, "region": None, "country": None})

    def test_count_capped_with_requested_count(self):
        payload = {**BUSINESS_PAYLOAD, "count": 5000}
        spec = _parse(payload, "get me 5000 dentists in Boise")
        self.assertEqual(spec["count"], parse_query.MAX_LEAD_COUNT)
        self.assertEqual(spec["requested_count"], 5000)

    def test_junk_count_falls_back_to_default(self):
        for junk in (0, -3, "many", None):
            spec = _parse({**BUSINESS_PAYLOAD, "count": junk})
            self.assertEqual(spec["count"], parse_query.DEFAULT_COUNT)

    def test_blank_query_raises_without_api_call(self):
        with mock.patch.object(parse_query, "_get_client") as client_getter:
            with self.assertRaises(ValueError):
                parse_query.parse_query("   ")
        client_getter.assert_not_called()


class RouterAndHelpersTestCase(unittest.TestCase):
    def test_route_source_deterministic(self):
        self.assertEqual(parse_query.route_source({"entity_type": "person"}),
                         "b2b_data_api")
        self.assertEqual(parse_query.route_source({"entity_type": "business"}),
                         "google_maps")
        # Junk / missing entity_type never reaches the person connector.
        self.assertEqual(parse_query.route_source({}), "google_maps")

    def test_spec_from_args_is_a_business_maps_spec(self):
        spec = parse_query.spec_from_args("dentist", "Austin TX", 50)
        self.assertEqual(spec["entity_type"], "business")
        self.assertEqual(spec["source"], "google_maps")
        self.assertEqual(spec["industry"], "dentist")
        self.assertEqual(spec["geo"]["city"], "Austin TX")
        self.assertEqual(spec["count"], 50)
        self.assertEqual(spec["confidence"], "high")
        self.assertIsNone(spec["raw_query"])

    def test_to_maps_params_flattens_geo_most_specific_first(self):
        spec = parse_query.spec_from_args("dentist", "Boise", 20)
        spec["geo"] = {"city": "Boise", "region": "ID", "country": "US"}
        self.assertEqual(parse_query.to_maps_params(spec),
                         {"category": "dentist", "city": "Boise ID", "limit": 20})
        spec["geo"] = {"city": None, "region": "Texas", "country": "US"}
        self.assertEqual(parse_query.to_maps_params(spec)["city"], "Texas")
        spec["geo"] = {"city": None, "region": None, "country": "US"}
        self.assertEqual(parse_query.to_maps_params(spec)["city"], "US")

    def test_to_maps_params_uses_keywords_when_no_industry(self):
        spec = parse_query.spec_from_args("x", "Boise", 10)
        spec["industry"] = None
        spec["keywords"] = ["coffee shop", "cafe"]
        self.assertEqual(parse_query.to_maps_params(spec)["category"],
                         "coffee shop cafe")

    def test_describe_spec(self):
        spec = parse_query.spec_from_args("dentists", "Boise ID", 20)
        spec["qualification_hints"] = "without online booking"
        self.assertEqual(parse_query.describe_spec(spec),
                         "dentists in Boise ID (max 20) — without online booking")
        person = dict(spec, entity_type="person", role="founders",
                      industry="marketing agencies", qualification_hints=None)
        person["geo"] = {"city": "Austin", "region": None, "country": None}
        self.assertEqual(parse_query.describe_spec(person),
                         "founders of marketing agencies in Austin (max 20)")

    def test_spec_is_unusable(self):
        empty = {"industry": None, "keywords": [], "role": None}
        self.assertTrue(parse_query.spec_is_unusable(empty))
        self.assertFalse(parse_query.spec_is_unusable({**empty, "industry": "dentists"}))
        self.assertFalse(parse_query.spec_is_unusable({**empty, "role": "founder"}))


if __name__ == "__main__":
    unittest.main()
