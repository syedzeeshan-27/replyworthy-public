"""The approve gate: scrape → pause → free list OR paid enrich.

Every test stubs run_lead_gen.scrape_phase (zero network, zero credits), so
the whole flow runs offline. Covers the funnel/preview render, the two forks,
cancel, the concurrency rules the per-workspace lock must satisfy, the
double-click guard, cross-workspace isolation, and that a failed scrape ends
'error', not a permanent 'running'.

Run (from the repo root):
    python -m unittest tests.test_job_gate -v
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp
from web import jobs as jobs_mod

try:
    from tests.tenancy import signup_owner, signup_tenant
except ImportError:
    from tenancy import signup_owner, signup_tenant

lead_db = webapp.lead_db

SCRAPED = [
    {"place_id": f"p{i}", "name": f"Practice {i}", "phone": f"555-10{i:02d}",
     "website": f"https://p{i}.example", "area": "Downtown",
     "rating": 4.5, "reviews_count": 30 + i}
    for i in range(15)
]


def fake_phase1(run_id="run_test", results=None, found=40, already_seen=0):
    results = SCRAPED if results is None else results
    return {
        "run_id": run_id,
        "spec": {"source": "google_maps", "industry": "dentist",
                 "geo": {"city": "Tampa, FL"}, "count": 200},
        "source": "google_maps",
        "spec_path": Path("/tmp/spec.json"),
        "raw_path": Path("/tmp/raw.json"),
        "results": list(results),
        "funnel_seed": {"found": found, "dropped_dupes": 3,
                        "dropped_chains": 2, "dropped_seen": 0},
        "found": found,
        "already_seen": already_seen,
        "truncated": 0,
        "seen_marked": False,
    }


def _wait(pred, timeout=3.0):
    """Spin until a background thread flips the job state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class GateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {
            "SESSION_SECRET": "test-secret", "UNSUBSCRIBE_SECRET": "test-unsub",
        })
        self._env.start()
        os.environ.pop("DATABASE_URL", None)
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        lead_db.DB_PATH = Path(self._tmp.name) / "leads.db"
        jobs_mod.JOBS.clear()
        self.client = TestClient(webapp.app)
        signup_owner(self.client)

    def tearDown(self) -> None:
        jobs_mod.JOBS.clear()
        lead_db.DB_PATH = self._orig_db_path
        self._tmp.cleanup()
        self._env.stop()

    def _own_ws(self) -> int:
        return lead_db.OWNER_WORKSPACE_ID  # first signup claims workspace 1

    def _start_to_gate(self, **phase_kwargs):
        """POST a business search and let the stubbed scrape park it at the gate."""
        with mock.patch.object(jobs_mod.run_lead_gen, "scrape_phase",
                               return_value=fake_phase1(**phase_kwargs)):
            r = self.client.post("/jobs", data={
                "category": "dentist", "city": "Tampa, FL", "limit": "200",
            }, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            job_id = r.headers["location"].split("/")[-1]
            self.assertTrue(_wait(
                lambda: jobs_mod.JOBS[job_id].status == "awaiting_choice"))
        return job_id


class GateRenderTestCase(GateTestBase):
    def test_business_search_pauses_at_the_gate(self):
        job_id = self._start_to_gate()
        self.assertEqual(jobs_mod.JOBS[job_id].status, "awaiting_choice")

    def test_gate_page_shows_count_funnel_preview_and_three_buttons(self):
        job_id = self._start_to_gate(found=40)
        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Found 40", r.text)                 # the real count
        self.assertIn("On your list", r.text)             # funnel
        self.assertIn("Preview", r.text)                  # the preview table
        self.assertIn("Practice 0", r.text)               # a real row
        self.assertIn(f"/jobs/{job_id}/list", r.text)
        self.assertIn(f"/jobs/{job_id}/continue", r.text)
        self.assertIn(f"/jobs/{job_id}/cancel", r.text)

    def test_gate_page_does_not_auto_refresh(self):
        # A meta-refresh would bounce a reader mid-click.
        job_id = self._start_to_gate()
        r = self.client.get(f"/jobs/{job_id}")
        self.assertNotIn("http-equiv=\"refresh\"", r.text)

    def test_already_seen_is_surfaced(self):
        job_id = self._start_to_gate(already_seen=12)
        r = self.client.get(f"/jobs/{job_id}")
        self.assertIn("12 of these you already found", r.text)

    def test_negative_search_assertions_hold_on_home(self):
        # The gate must not reintroduce the removed parser form's names.
        r = self.client.get("/")
        for gone in ('action="/search"', 'name="query"', 'name="spec_json"',
                     "Advanced mode"):
            self.assertNotIn(gone, r.text)


class ListForkTestCase(GateTestBase):
    def test_list_fork_charges_nothing_and_downloads_bytes(self):
        job_id = self._start_to_gate()
        with mock.patch.object(webapp.billing, "debit_leads") as debit:
            r = self.client.post(f"/jobs/{job_id}/list", follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertTrue(_wait(
                lambda: jobs_mod.JOBS[job_id].status == "done"))
        debit.assert_not_called()

        job = jobs_mod.JOBS[job_id]
        self.assertEqual(job.phase, "list")
        self.assertEqual(job.credits_charged, 0)

        dl = self.client.get(f"/jobs/{job_id}/download")
        self.assertEqual(dl.status_code, 200)
        self.assertIn("spreadsheetml.sheet", dl.headers["content-type"])
        self.assertTrue(dl.content.startswith(b"PK"))  # a real xlsx

    def test_list_fork_writes_no_db_rows(self):
        job_id = self._start_to_gate()
        self.client.post(f"/jobs/{job_id}/list", follow_redirects=False)
        self.assertTrue(_wait(lambda: jobs_mod.JOBS[job_id].status == "done"))
        self.assertEqual(lead_db.get_leads(self._own_ws()), [])

    def test_download_is_the_list_layout(self):
        import io
        from openpyxl import load_workbook
        from save_to_excel import LIST_COLUMNS
        job_id = self._start_to_gate()
        self.client.post(f"/jobs/{job_id}/list", follow_redirects=False)
        self.assertTrue(_wait(lambda: jobs_mod.JOBS[job_id].status == "done"))
        dl = self.client.get(f"/jobs/{job_id}/download")
        ws = load_workbook(io.BytesIO(dl.content))["Leads"]
        headers = [c.value for c in ws[1]]
        self.assertEqual(headers, [h for h, _k, _w in LIST_COLUMNS])


class ContinueForkTestCase(GateTestBase):
    def test_continue_runs_enrich_and_double_click_spawns_one_phase2(self):
        job_id = self._start_to_gate()
        calls = []

        def fake_enrich(phase1, workspace_id, us_only=False, offer_id=None):
            calls.append(phase1["run_id"])
            time.sleep(0.05)
            return {"total_found": 40, "funnel": {"found": 40, "delivered": 10},
                    "source": "google_maps", "hot_leads": 3,
                    "credits_charged": 10, "output_path": None}

        with mock.patch.object(jobs_mod.run_lead_gen, "enrich_phase", fake_enrich), \
             mock.patch.object(webapp.billing, "run_guard", return_value=None):
            r1 = self.client.post(f"/jobs/{job_id}/continue", follow_redirects=False)
            r2 = self.client.post(f"/jobs/{job_id}/continue", follow_redirects=False)
            self.assertEqual(r1.status_code, 303)
            self.assertTrue(_wait(lambda: jobs_mod.JOBS[job_id].status == "done"))
        self.assertEqual(len(calls), 1, "the CAS gate must block the 2nd fork")

    def test_continue_blocked_when_credits_drained(self):
        job_id = self._start_to_gate()
        with mock.patch.object(webapp.billing, "run_guard",
                               return_value="Out of credits — top up to run this."), \
             mock.patch.object(jobs_mod.run_lead_gen, "enrich_phase") as enrich:
            r = self.client.post(f"/jobs/{job_id}/continue", follow_redirects=False)
        self.assertEqual(r.status_code, 402)
        self.assertIn("Out of credits", r.text)
        enrich.assert_not_called()
        self.assertEqual(jobs_mod.JOBS[job_id].status, "awaiting_choice")


class CancelTestCase(GateTestBase):
    def test_cancel_discards_the_gate_with_no_charge(self):
        job_id = self._start_to_gate()
        with mock.patch.object(webapp.billing, "debit_leads") as debit:
            r = self.client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        debit.assert_not_called()
        job = jobs_mod.JOBS[job_id]
        self.assertEqual(job.status, "canceled")
        self.assertIsNone(job.scrape)
        self.assertEqual(lead_db.get_leads(self._own_ws()), [])


class ConcurrencyTestCase(GateTestBase):
    def test_a_gated_job_does_not_block_a_new_search(self):
        first = self._start_to_gate()
        self.assertEqual(jobs_mod.JOBS[first].status, "awaiting_choice")
        # A second search in the SAME workspace must still start.
        second = self._start_to_gate()
        self.assertNotEqual(first, second)
        self.assertEqual(jobs_mod.JOBS[second].status, "awaiting_choice")

    def test_stale_gate_expires(self):
        job_id = self._start_to_gate()
        job = jobs_mod.JOBS[job_id]
        job.gate_expires_at = job.started_at  # already in the past
        # Any lookup runs the sweep.
        self.assertIsNotNone(jobs_mod.get_job(job_id, self._own_ws()))
        self.assertEqual(jobs_mod.JOBS[job_id].status, "canceled")

    def test_forks_are_workspace_scoped(self):
        job_id = self._start_to_gate()
        other = TestClient(webapp.app)
        signup_tenant(other)
        for verb in ("list", "continue", "cancel"):
            r = other.post(f"/jobs/{job_id}/{verb}", follow_redirects=False)
            # Another workspace can't see the job — it's told the preview is
            # gone (409), never allowed to act on it.
            self.assertIn(r.status_code, (409, 404))
        self.assertEqual(jobs_mod.JOBS[job_id].status, "awaiting_choice")


class ScrapeFailureTestCase(GateTestBase):
    def test_systemexit_in_scrape_ends_error_not_running(self):
        def boom(*a, **k):
            raise SystemExit("step failed")  # what run_step raises

        with mock.patch.object(jobs_mod.run_lead_gen, "scrape_phase", boom):
            r = self.client.post("/jobs", data={
                "category": "dentist", "city": "Tampa, FL", "limit": "50",
            }, follow_redirects=False)
            job_id = r.headers["location"].split("/")[-1]
            self.assertTrue(_wait(
                lambda: jobs_mod.JOBS[job_id].status == "error"))
        self.assertEqual(jobs_mod.JOBS[job_id].status, "error")


if __name__ == "__main__":
    unittest.main()
