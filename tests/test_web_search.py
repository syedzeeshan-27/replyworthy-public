"""Web-layer tests for the lead search form and the job status page.

The freeform "describe it in your own words" box was removed (2026-07-23):
the structured form is the only way to start a run, so nothing here should
ever reach tools/parse_query's Claude call. The person path still exists —
it is a lead-type choice on the same form, and both honest gates (plan
capability, then B2B account) are asserted below.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp

lead_db = webapp.lead_db          # the exact module objects the routes use
parse_query = webapp.parse_query


class WebSearchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {
            "SESSION_SECRET": "test-secret",
            "UNSUBSCRIBE_SECRET": "test-unsub",
        })
        self._env.start()
        os.environ.pop("DATABASE_URL", None)
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        lead_db.DB_PATH = Path(self._tmp.name) / "leads.db"
        self.client = TestClient(webapp.app)
        # Routes are session-gated since V2 Stage 4 — sign in as the owner.
        self.client.post("/signup", data={
            "email": "owner@example.com", "password": "ownerpass123",
            "workspace_name": "Owner HQ",
        }, follow_redirects=False)

    def tearDown(self) -> None:
        lead_db.DB_PATH = self._orig_db_path
        self._tmp.cleanup()
        self._env.stop()

    # --- the search form ----------------------------------------------------

    def test_dashboard_offers_one_structured_form_and_no_freeform_box(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('action="/jobs"', r.text)
        for field in ('name="lead_type"', 'name="category"', 'name="city"',
                      'name="limit"'):
            self.assertIn(field, r.text)
        # The parser entry point and its confirm panel are gone for good.
        self.assertNotIn('action="/search"', r.text)
        self.assertNotIn("Advanced mode", r.text)
        self.assertNotIn('name="query"', r.text)
        self.assertNotIn('name="spec_json"', r.text)

    def test_the_search_route_no_longer_exists(self):
        r = self.client.post("/search", data={"query": "dentists in Boise"})
        self.assertEqual(r.status_code, 404)

    def test_us_phone_filter_is_no_longer_a_choice_but_stays_on(self):
        """The checkbox went away; the run must still ask for US numbers,
        or every phoneless listing would burn enrichment credit."""
        self.assertNotIn('name="us_only"', self.client.get("/").text)
        with mock.patch.object(webapp, "start_job", return_value="fx1") as m:
            self.client.post("/jobs", data={
                "category": "dentist", "city": "Austin TX", "limit": "10",
            }, follow_redirects=False)
        self.assertIs(m.call_args.args[4], True)   # the us_only argument

    def test_business_search_starts_a_job_without_a_spec(self):
        with mock.patch.object(webapp, "start_job", return_value="fx2") as m:
            r = self.client.post("/jobs", data={
                "category": "dentist", "city": "Austin TX", "limit": "10",
            }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(m.call_args.args[1:4], ("dentist", "Austin TX", 10))
        self.assertIsNone(m.call_args.kwargs["spec"])

    def test_form_never_calls_the_parser(self):
        with mock.patch.object(parse_query, "parse_query") as parser, \
             mock.patch.object(webapp, "start_job", return_value="fx3"):
            self.client.post("/jobs", data={
                "category": "dentist", "city": "Austin TX", "limit": "10",
            }, follow_redirects=False)
        parser.assert_not_called()

    def test_missing_category_or_city_is_refused(self):
        with mock.patch.object(webapp, "start_job") as m:
            r = self.client.post("/jobs", data={"category": "dentist"})
        m.assert_not_called()
        self.assertEqual(r.status_code, 400)
        self.assertIn("required", r.text)

    # --- the person path (same form, different lead type) --------------------

    def test_person_search_without_account_says_so_and_runs_nothing(self):
        with mock.patch.object(webapp, "b2b_ready", return_value=False), \
             mock.patch.object(webapp, "start_job") as m:
            r = self.client.post("/jobs", data={
                "lead_type": "person", "category": "marketing agencies",
                "city": "Austin TX", "limit": "10",
            })
        m.assert_not_called()
        self.assertEqual(r.status_code, 400)
        self.assertIn("needs the contact-database account", r.text)

    def test_person_search_with_account_routes_to_the_b2b_source(self):
        with mock.patch.object(webapp, "b2b_ready", return_value=True), \
             mock.patch.object(webapp, "start_job", return_value="fx4") as m:
            r = self.client.post("/jobs", data={
                "lead_type": "person", "category": "marketing agencies",
                "city": "Austin TX", "limit": "10",
            }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        spec = m.call_args.kwargs["spec"]
        self.assertEqual(spec["entity_type"], "person")
        self.assertEqual(spec["source"], "b2b_data_api")   # router decided
        self.assertEqual(spec["industry"], "marketing agencies")
        self.assertEqual(spec["geo"]["city"], "Austin TX")
        self.assertEqual(spec["count"], 10)

    # --- job status page: the lead funnel ------------------------------------

    def test_status_page_shows_funnel_breakdown(self):
        from web.jobs import JOBS, Job
        job = Job(id="fxfunnel1", category="real estate agencies",
                  city="Austin TX", limit=5, status="done",
                  total_found=5, hot_leads=2,
                  funnel={"found": 5, "dropped_chains": 1, "dropped_seen": 0,
                          "skipped_no_contact": 0, "excluded_equipped": 1,
                          "excluded_non_us": 0, "delivered": 3})
        JOBS[job.id] = job
        try:
            r = self.client.get(f"/jobs/{job.id}")
        finally:
            JOBS.pop(job.id, None)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Found on Google Maps", r.text)
        self.assertIn("Delivered to you", r.text)
        self.assertIn("filtered out 2", r.text)
        self.assertIn("chains or duplicate listings", r.text)
        self.assertIn("already have chat or booking software", r.text)
        self.assertNotIn("no US phone number", r.text)  # zero-count rows hidden

    def test_status_page_person_run_words_funnel_for_the_database(self):
        from web.jobs import JOBS, Job
        job = Job(id="fxperson1", category="founder of marketing agencies",
                  city="Austin TX", limit=25, status="done",
                  total_found=25, hot_leads=4, source="b2b_data_api",
                  funnel={"found": 25, "dropped_chains": 0, "dropped_seen": 0,
                          "skipped_no_contact": 2, "excluded_equipped": 0,
                          "excluded_non_us": 0, "delivered": 23})
        JOBS[job.id] = job
        try:
            r = self.client.get(f"/jobs/{job.id}")
        finally:
            JOBS.pop(job.id, None)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Found in the contact database", r.text)
        self.assertNotIn("Found on Google Maps", r.text)

    def test_lead_detail_renders_person_fields(self):
        lead_db.upsert_leads(1, [{
            "entity_type": "person", "name": "Jane Marsh", "title": "Founder",
            "company": "Marsh Marketing",
            "linkedin_url": "https://www.linkedin.com/in/jane-marsh",
            "email": "jane@marshmarketing.example", "score": 4,
            "website": "https://marshmarketing.example",
            "source": "b2b_data_api",
        }])
        lead_id = lead_db.get_leads(1)[0]["id"]
        r = self.client.get(f"/lead/{lead_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Founder", r.text)
        self.assertIn("Marsh Marketing", r.text)
        self.assertIn("linkedin.com/in/jane-marsh", r.text)
        self.assertNotIn("reviews)", r.text)  # business-only row hidden

    def test_status_page_without_funnel_falls_back_to_old_table(self):
        from web.jobs import JOBS, Job
        job = Job(id="fxfunnel2", category="dentist", city="Boise",
                  limit=5, status="done", total_found=3, hot_leads=1)
        JOBS[job.id] = job
        try:
            r = self.client.get(f"/jobs/{job.id}")
        finally:
            JOBS.pop(job.id, None)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Total leads", r.text)


if __name__ == "__main__":
    unittest.main()
