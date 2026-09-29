"""Tests for the widened Apify actor input and dataset pagination
(tools/sources/google_maps.py).

Background: a search for 200 dental clinics in Tampa returned 33. The actor
input asked ONE question — a single "category city" string with no location
field — and `maxCrawledPlacesPerSearch` is a ceiling per search term, not a
target. These tests pin the shape that fixes it.

All offline: the Apify HTTP layer is stubbed, no token needed.

Run (from the repo root):
    python -m unittest tests.test_actor_input -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from sources import google_maps as gm  # noqa: E402


class _NoSynonyms(unittest.TestCase):
    """Isolates term-shape assertions from config/category_synonyms.json, so
    editing the shipped synonym list can't break unrelated tests. The real
    file gets its own coverage in ShippedSynonymsTestCase."""

    def setUp(self):
        patcher = mock.patch.object(gm, "_load_synonyms", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)


class SearchTermsTestCase(_NoSynonyms):
    def test_category_always_leads(self):
        self.assertEqual(gm.search_terms("dentist")[0], "dentist")

    def test_keywords_follow_the_category(self):
        terms = gm.search_terms("dentist", ["dental clinic", "dental office"])
        self.assertEqual(terms[:3], ["dentist", "dental clinic", "dental office"])

    def test_dedupes_case_insensitively_preserving_order(self):
        terms = gm.search_terms("Dentist", ["dentist", "DENTIST", "orthodontist"])
        self.assertEqual(terms, ["Dentist", "orthodontist"])

    def test_capped_at_max_terms(self):
        terms = gm.search_terms("dentist", [f"kw{i}" for i in range(20)])
        self.assertEqual(len(terms), gm.MAX_TERMS)

    def test_blank_keywords_ignored(self):
        self.assertEqual(gm.search_terms("dentist", ["", "  ", None]), ["dentist"])

    def test_always_returns_at_least_one_term(self):
        self.assertEqual(len(gm.search_terms("dentist", [])), 1)


class ActorInputTestCase(_NoSynonyms):
    def test_city_travels_in_location_query_not_the_search_text(self):
        payload = gm.build_actor_input("dentist", "Tampa, FL", 200)
        self.assertEqual(payload["locationQuery"], "Tampa, FL")
        self.assertEqual(payload["searchStringsArray"], ["dentist"])

    def test_deeper_city_scrape_is_not_a_real_field(self):
        """It does not exist in the actor's input schema — verified against
        the published schema. Sending it would be silently ignored."""
        self.assertNotIn("deeperCityScrape",
                         gm.build_actor_input("dentist", "Tampa, FL", 200))

    def test_per_term_budget_divides_the_overscraped_limit(self):
        payload = gm.build_actor_input("dentist", "Tampa, FL", 200,
                                       ["dental clinic", "dental office", "orthodontist"])
        self.assertEqual(len(payload["searchStringsArray"]), 4)
        # ceil(200 * 1.6 / 4) == 80
        self.assertEqual(payload["maxCrawledPlacesPerSearch"], 80)

    def test_small_limits_still_get_a_workable_floor(self):
        payload = gm.build_actor_input("dentist", "Tampa, FL", 5)
        self.assertGreaterEqual(payload["maxCrawledPlacesPerSearch"], gm.MIN_PER_TERM)

    def test_per_term_is_never_zero(self):
        for limit in (1, 2, 3, 10, 50, 500):
            payload = gm.build_actor_input("dentist", "Tampa, FL", limit)
            self.assertGreaterEqual(payload["maxCrawledPlacesPerSearch"], 1)

    def test_preserved_settings(self):
        payload = gm.build_actor_input("dentist", "Tampa, FL", 50)
        self.assertEqual(payload["language"], "en")
        self.assertIs(payload["skipClosedPlaces"], True)
        self.assertIs(payload["scrapeContacts"], True)

    def test_fold_city_fallback_restores_the_old_string_shape(self):
        with mock.patch.object(gm, "FOLD_CITY_INTO_TERMS", True):
            payload = gm.build_actor_input("dentist", "Tampa, FL", 50,
                                           ["dental clinic"])
        self.assertEqual(payload["searchStringsArray"],
                         ["dentist Tampa, FL", "dental clinic Tampa, FL"])
        self.assertEqual(payload["locationQuery"], "Tampa, FL")


class ShippedSynonymsTestCase(unittest.TestCase):
    """config/category_synonyms.json is real config the search depends on."""

    def test_file_parses_and_every_value_is_a_list(self):
        data = gm._load_synonyms()
        self.assertTrue(data, "the shipped synonyms file should load")
        for key, value in data.items():
            self.assertIsInstance(value, list, f"{key} must map to a list")
            for term in value:
                self.assertIsInstance(term, str)
                self.assertTrue(term.strip(), f"{key} has a blank term")

    def test_comment_string_is_not_loaded_as_terms(self):
        """A string value would be appended one character at a time."""
        self.assertNotIn("_comment", gm._load_synonyms())

    def test_the_tampa_case_fans_out(self):
        """The search that returned 33: one term became several."""
        terms = gm.search_terms("dental clinics")
        self.assertEqual(terms[0], "dental clinics")
        self.assertGreater(len(terms), 1)

    def test_missing_file_is_not_an_error(self):
        with mock.patch.object(gm, "SYNONYMS_PATH", Path("does-not-exist.json")):
            self.assertEqual(gm._load_synonyms(), {})
            self.assertEqual(gm.search_terms("dentist"), ["dentist"])


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FetchItemsPaginationTestCase(unittest.TestCase):
    """The old single unpaginated GET truncated large runs silently."""

    def _stub(self, total):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params)
            offset, limit = params["offset"], params["limit"]
            page = [{"placeId": f"p{i}"}
                    for i in range(offset, min(offset + limit, total))]
            return _StubResponse(page)

        return fake_get, calls

    def test_single_page_run_makes_exactly_one_request(self):
        fake_get, calls = self._stub(10)
        with mock.patch.object(gm.requests, "get", fake_get):
            items = gm.fetch_items("tok", "ds")
        self.assertEqual(len(items), 10)
        self.assertEqual(len(calls), 1)

    def test_multi_page_run_walks_offsets_in_order(self):
        fake_get, calls = self._stub(2500)
        with mock.patch.object(gm.requests, "get", fake_get):
            items = gm.fetch_items("tok", "ds")
        self.assertEqual(len(items), 2500)
        self.assertEqual([c["offset"] for c in calls], [0, 1000, 2000])

    def test_no_duplicate_items_across_pages(self):
        fake_get, _ = self._stub(2500)
        with mock.patch.object(gm.requests, "get", fake_get):
            items = gm.fetch_items("tok", "ds")
        ids = [i["placeId"] for i in items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_max_items_caps_the_walk(self):
        fake_get, calls = self._stub(5000)
        with mock.patch.object(gm.requests, "get", fake_get):
            items = gm.fetch_items("tok", "ds", max_items=1500)
        self.assertEqual(len(items), 1500)
        self.assertEqual(sum(c["limit"] for c in calls), 1500)

    def test_hard_ceiling_protects_memory(self):
        fake_get, _ = self._stub(999_999)
        with mock.patch.object(gm.requests, "get", fake_get):
            items = gm.fetch_items("tok", "ds", max_items=99_999)
        self.assertEqual(len(items), gm.MAX_DATASET_ITEMS)

    def test_empty_dataset_returns_empty_list(self):
        fake_get, _ = self._stub(0)
        with mock.patch.object(gm.requests, "get", fake_get):
            self.assertEqual(gm.fetch_items("tok", "ds"), [])


class RunSearchOrderTestCase(_NoSynonyms):
    """run_search must dedupe BEFORE the chain filter, annotate the seen
    ledger without dropping when asked, and truncate last."""

    def _run(self, raw_items, limit=50, **kwargs):
        with mock.patch.object(gm, "require", return_value="tok"), \
             mock.patch.object(gm, "start_run", return_value={"id": "r1"}), \
             mock.patch.object(gm, "wait_for_run",
                               return_value={"status": "SUCCEEDED",
                                             "defaultDatasetId": "ds"}), \
             mock.patch.object(gm, "fetch_items", return_value=raw_items), \
             mock.patch.object(gm, "load_seen", return_value={}), \
             mock.patch.object(gm, "mark_seen") as marked:
            search = gm.run_search("dentist", "Tampa, FL", limit, **kwargs)
        return search, marked

    def test_fan_out_duplicates_are_collapsed_not_treated_as_chains(self):
        raw = [{"placeId": f"p{i}", "title": f"Practice {i}",
                "website": f"https://p{i}.example"} for i in range(10)]
        raw += raw[:4]  # the same 4 places returned by a second search term
        search, _ = self._run(raw)
        self.assertEqual(search["found"], 14)
        self.assertEqual(search["dropped_dupes"], 4)
        self.assertEqual(len(search["results"]), 10,
                         "duplicates must not take real leads down with them")

    def test_truncates_to_limit_after_hygiene(self):
        raw = [{"placeId": f"p{i}", "title": f"Practice {i}"} for i in range(30)]
        search, _ = self._run(raw, limit=10)
        self.assertEqual(len(search["results"]), 10)
        self.assertEqual(search["truncated"], 20)

    def test_filter_seen_false_annotates_but_never_drops_or_marks(self):
        raw = [{"placeId": f"p{i}", "title": f"Practice {i}"} for i in range(5)]
        with mock.patch.object(gm, "require", return_value="tok"), \
             mock.patch.object(gm, "start_run", return_value={"id": "r1"}), \
             mock.patch.object(gm, "wait_for_run",
                               return_value={"status": "SUCCEEDED",
                                             "defaultDatasetId": "ds"}), \
             mock.patch.object(gm, "fetch_items", return_value=raw), \
             mock.patch.object(gm, "load_seen", return_value={"p0": {}, "p1": {}}), \
             mock.patch.object(gm, "mark_seen") as marked:
            search = gm.run_search("dentist", "Tampa, FL", 50, filter_seen=False)

        self.assertEqual(len(search["results"]), 5, "nothing dropped")
        self.assertEqual(search["already_seen"], 2)
        self.assertEqual(search["dropped_seen"], 0)
        marked.assert_not_called()
        self.assertTrue(search["results"][0]["seen_before"])
        self.assertFalse(search["results"][4]["seen_before"])

    def test_filter_seen_true_keeps_the_old_behaviour(self):
        raw = [{"placeId": f"p{i}", "title": f"Practice {i}"} for i in range(5)]
        with mock.patch.object(gm, "require", return_value="tok"), \
             mock.patch.object(gm, "start_run", return_value={"id": "r1"}), \
             mock.patch.object(gm, "wait_for_run",
                               return_value={"status": "SUCCEEDED",
                                             "defaultDatasetId": "ds"}), \
             mock.patch.object(gm, "fetch_items", return_value=raw), \
             mock.patch.object(gm, "load_seen", return_value={"p0": {}}), \
             mock.patch.object(gm, "mark_seen") as marked:
            search = gm.run_search("dentist", "Tampa, FL", 50)

        self.assertEqual(len(search["results"]), 4)
        self.assertEqual(search["dropped_seen"], 1)
        marked.assert_called_once()

    def test_keywords_reach_the_actor_input(self):
        with mock.patch.object(gm, "require", return_value="tok"), \
             mock.patch.object(gm, "start_run", return_value={"id": "r1"}) as started, \
             mock.patch.object(gm, "wait_for_run",
                               return_value={"status": "SUCCEEDED",
                                             "defaultDatasetId": "ds"}), \
             mock.patch.object(gm, "fetch_items", return_value=[]), \
             mock.patch.object(gm, "load_seen", return_value={}), \
             mock.patch.object(gm, "mark_seen"):
            gm.run_search("dentist", "Tampa, FL", 50, keywords=["dental clinic"])

        payload = started.call_args.args[1]
        self.assertEqual(payload["searchStringsArray"], ["dentist", "dental clinic"])


if __name__ == "__main__":
    unittest.main()
