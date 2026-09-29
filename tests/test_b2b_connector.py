"""Tests for the B2B data API connector (tools/sources/b2b_data_api.py,
V2 Stage 3 plan v2/03 C2/E).

Everything runs against a FAKE transport — zero live API calls, zero paid
calls, no key needed. Covers: spec→params mapping, normalization into the
unified RawLead schema, pagination stopping at the spec's cap, rate-limit
backoff, loud auth failure, missing-email handling, in-batch dedupe.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from sources import b2b_data_api as b2b  # noqa: E402

FAKE_ENV = {
    "B2B_DATA_API_KEY": "test-key-not-real",
    "B2B_DATA_API_URL": "https://fake-b2b.example/v1/people/search",
}

PERSON_SPEC = {
    "entity_type": "person",
    "source": "b2b_data_api",
    "industry": "marketing agencies",
    "keywords": ["marketing agency"],
    "geo": {"city": "Austin", "region": "TX", "country": None},
    "role": "founder",
    "seniority": "owner",
    "count": 25,
    "qualification_hints": None,
    "unsupported": [],
    "confidence": "high",
    "raw_query": "founders of marketing agencies in Austin, 25",
}

RAW_ITEM = {
    "id": 12345,
    "full_name": "Jane Marsh",
    "job_title": "Founder",
    "company_name": "Marsh Marketing",
    "company_website": "https://marshmarketing.example/?utm_source=directory",
    "linkedin_url": "https://www.linkedin.com/in/jane-marsh",
    "email": "jane@marshmarketing.example",
    "phone_numbers": ["+1 512 555 0100"],
    "city": "Austin",
}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Queue of responses; records every request made."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError("connector made more requests than the test queued")
        return self._responses.pop(0)


def _items(n: int, start: int = 0) -> list[dict]:
    return [{**RAW_ITEM, "id": start + i,
             "email": f"person{start + i}@example.com"} for i in range(n)]


class SpecToParamsTestCase(unittest.TestCase):
    def test_full_spec_maps_all_fields(self):
        params = b2b.spec_to_params(PERSON_SPEC, page=2, per_page=10)
        self.assertEqual(params["page"], 2)
        self.assertEqual(params["per_page"], 10)
        self.assertEqual(params["person_titles"], ["founder"])
        self.assertEqual(params["person_seniorities"], ["owner"])
        self.assertEqual(params["company_industries"], ["marketing agencies"])
        self.assertEqual(params["person_locations"], ["Austin TX"])

    def test_absent_fields_are_not_sent(self):
        spec = {**PERSON_SPEC, "role": None, "seniority": None,
                "industry": None, "keywords": [],
                "geo": {"city": None, "region": None, "country": None}}
        params = b2b.spec_to_params(spec, page=1, per_page=25)
        for key in ("person_titles", "person_seniorities",
                    "company_industries", "keywords", "person_locations"):
            self.assertNotIn(key, params)


class SearchPeopleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, FAKE_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_happy_path_normalizes_to_unified_schema(self):
        session = FakeSession([FakeResponse(200, {"results": [dict(RAW_ITEM)]})])
        spec = {**PERSON_SPEC, "count": 1}
        search = b2b.run_search(spec, session=session)
        self.assertEqual(search["contacts_pulled"], 1)
        lead = search["results"][0]
        self.assertEqual(lead["entity_type"], "person")
        self.assertEqual(lead["name"], "Jane Marsh")
        self.assertEqual(lead["title"], "Founder")
        self.assertEqual(lead["company"], "Marsh Marketing")
        # Tracking params stripped, same hygiene as Maps websites.
        self.assertEqual(lead["website"], "https://marshmarketing.example/")
        self.assertEqual(lead["linkedin_url"], RAW_ITEM["linkedin_url"])
        self.assertEqual(lead["email"], "jane@marshmarketing.example")
        self.assertEqual(lead["email_source"], "api")
        self.assertEqual(lead["phone"], "+1 512 555 0100")
        self.assertEqual(lead["source"], "b2b_data_api")
        self.assertEqual(lead["source_id"], "12345")
        self.assertEqual(lead["source_raw"], RAW_ITEM)
        # The key went in a header, never a URL param.
        self.assertEqual(session.calls[0]["headers"]["X-Api-Key"],
                         "test-key-not-real")

    def test_pagination_stops_at_cap_even_if_api_pages_further(self):
        # Cap 30: page 1 returns a full 25, page 2 must ask for only 5 —
        # and even though the API sends 25 more, exactly 30 come back.
        session = FakeSession([
            FakeResponse(200, {"results": _items(25)}),
            FakeResponse(200, {"results": _items(25, start=25)}),
        ])
        spec = {**PERSON_SPEC, "count": 30}
        search = b2b.run_search(spec, session=session)
        self.assertEqual(len(search["results"]), 30)
        self.assertEqual(search["contacts_pulled"], 30)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["json"]["per_page"], 25)
        self.assertEqual(session.calls[1]["json"]["per_page"], 5)

    def test_short_page_stops_without_extra_request(self):
        session = FakeSession([FakeResponse(200, {"results": _items(3)})])
        spec = {**PERSON_SPEC, "count": 50}
        search = b2b.run_search(spec, session=session)
        self.assertEqual(len(search["results"]), 3)
        self.assertEqual(len(session.calls), 1)

    def test_empty_result_returns_empty_list(self):
        session = FakeSession([FakeResponse(200, {"results": []})])
        search = b2b.run_search({**PERSON_SPEC, "count": 10}, session=session)
        self.assertEqual(search["results"], [])

    def test_rate_limit_backs_off_then_succeeds(self):
        session = FakeSession([
            FakeResponse(429),
            FakeResponse(200, {"results": _items(1)}),
        ])
        with mock.patch.object(b2b.time, "sleep") as fake_sleep:
            search = b2b.run_search({**PERSON_SPEC, "count": 1}, session=session)
        self.assertEqual(len(search["results"]), 1)
        fake_sleep.assert_called_once_with(b2b.BACKOFF_BASE)

    def test_persistent_rate_limit_fails_loudly(self):
        session = FakeSession([FakeResponse(429)] * (b2b.MAX_429_RETRIES + 1))
        with mock.patch.object(b2b.time, "sleep"):
            with self.assertRaises(RuntimeError):
                b2b.run_search({**PERSON_SPEC, "count": 1}, session=session)

    def test_auth_error_raises_never_degrades(self):
        session = FakeSession([FakeResponse(401)])
        with self.assertRaises(RuntimeError) as ctx:
            b2b.run_search({**PERSON_SPEC, "count": 1}, session=session)
        self.assertIn("B2B_DATA_API_KEY", str(ctx.exception))

    def test_missing_email_lead_still_lands_unenrollable(self):
        item = {**RAW_ITEM, "email": None}
        session = FakeSession([FakeResponse(200, {"results": [item]})])
        search = b2b.run_search({**PERSON_SPEC, "count": 1}, session=session)
        lead = search["results"][0]
        self.assertIsNone(lead["email"])
        self.assertIsNone(lead["email_source"])
        self.assertEqual(lead["name"], "Jane Marsh")  # delivered anyway

    def test_in_batch_dedupe_by_email(self):
        dupe = dict(RAW_ITEM)
        session = FakeSession([FakeResponse(200, {"results": [dupe, dict(dupe)]})])
        search = b2b.run_search({**PERSON_SPEC, "count": 2}, session=session)
        self.assertEqual(len(search["results"]), 1)
        self.assertEqual(search["contacts_pulled"], 2)  # both were billable

    def test_alternate_response_key_and_name_fields(self):
        item = {"id": "x1", "first_name": "Sam", "last_name": "Ortiz",
                "title": "Owner", "company": "Ortiz Media"}
        session = FakeSession([FakeResponse(200, {"data": [item]})])
        search = b2b.run_search({**PERSON_SPEC, "count": 1}, session=session)
        lead = search["results"][0]
        self.assertEqual(lead["name"], "Sam Ortiz")
        self.assertEqual(lead["company"], "Ortiz Media")

    def test_missing_key_fails_before_any_request(self):
        session = FakeSession([])
        env = {**FAKE_ENV, "B2B_DATA_API_KEY": ""}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(RuntimeError):
                b2b.run_search(PERSON_SPEC, session=session)
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
