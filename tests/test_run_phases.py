"""Tests for the two-phase pipeline split (run_lead_gen.py).

The gate runs the scrape, pauses, and lets the owner fork into a free list or
the paid enrichment. These tests pin the phase boundaries:
  * scrape_phase — Apify only: no Claude, no debit, no ledger marking, no DB.
  * list_only    — free fork: marks the ledger, no outputs/, no DB, 0 credits.
  * enrich_phase — reads its OWN run-scoped file, never a decoy left by another
    run (the stale-.tmp regression).

All offline: the paid connector and the enrich/save subprocesses are stubbed.

Run (from the repo root):
    python -m unittest tests.test_run_phases -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import run_lead_gen  # noqa: E402
import parse_query  # noqa: E402
import lead_db as flat_lead_db  # noqa: E402
import tools.lead_db as pkg_lead_db  # noqa: E402
from sources import google_maps as gm  # noqa: E402

SCRAPED = [
    {"place_id": "p1", "name": "Bright Smile Dental", "phone": "555-1000",
     "website": "https://brightsmile.example", "area": "Downtown",
     "rating": 4.8, "reviews_count": 90},
    {"place_id": "p2", "name": "Boise Family Dentistry", "phone": "555-2000",
     "website": "https://boisefam.example", "area": "North End",
     "rating": 4.2, "reviews_count": 40},
]


def fake_search(results, **overrides):
    base = {
        "results": list(results), "found": len(results), "dropped_dupes": 0,
        "dropped_chains": 0, "dropped_seen": 0, "already_seen": 0,
        "truncated": 0, "raw": [],
    }
    base.update(overrides)
    return base


class PhaseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        (base / "tmp").mkdir()
        (base / "out").mkdir()
        self._patches = [
            mock.patch.object(run_lead_gen, "TMP_DIR", base / "tmp"),
            mock.patch.object(run_lead_gen, "OUTPUTS_DIR", base / "out"),
            mock.patch.object(run_lead_gen, "_preflight"),
            mock.patch.object(flat_lead_db, "DB_PATH", base / "leads.db"),
            mock.patch.object(pkg_lead_db, "DB_PATH", base / "leads.db"),
        ]
        for p in self._patches:
            p.start()
        self.tmp = run_lead_gen.TMP_DIR

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _spec(self):
        spec = parse_query.spec_from_args("dentist", "Boise ID", 50)
        return spec


class ScrapePhaseTestCase(PhaseTestBase):
    def test_scrape_phase_makes_no_claude_call_no_debit_no_mark(self):
        with mock.patch.object(gm, "run_search",
                               return_value=fake_search(SCRAPED)) as search, \
             mock.patch.object(run_lead_gen.billing, "debit_leads") as debit, \
             mock.patch("seen_ledger.mark_seen") as marked:
            phase1 = run_lead_gen.scrape_phase(self._spec(), workspace_id=1)

        search.assert_called_once()
        # filter_seen defaults False for the gate — nothing dropped or marked.
        self.assertFalse(search.call_args.kwargs["filter_seen"])
        marked.assert_not_called()
        debit.assert_not_called()
        self.assertEqual(len(phase1["results"]), 2)
        self.assertFalse(phase1["seen_marked"])

    def test_scrape_phase_writes_a_run_scoped_raw_file(self):
        with mock.patch.object(gm, "run_search", return_value=fake_search(SCRAPED)):
            phase1 = run_lead_gen.scrape_phase(self._spec(), workspace_id=1)
        self.assertTrue(phase1["raw_path"].exists())
        self.assertIn(phase1["run_id"], phase1["raw_path"].name)

    def test_scrape_phase_writes_no_db_rows(self):
        with mock.patch.object(gm, "run_search", return_value=fake_search(SCRAPED)):
            run_lead_gen.scrape_phase(self._spec(), workspace_id=1)
        self.assertEqual(flat_lead_db.get_leads(1), [])

    def test_two_scrapes_get_distinct_run_ids(self):
        with mock.patch.object(gm, "run_search", return_value=fake_search(SCRAPED)):
            a = run_lead_gen.scrape_phase(self._spec(), workspace_id=1)
            b = run_lead_gen.scrape_phase(self._spec(), workspace_id=1)
        self.assertNotEqual(a["run_id"], b["run_id"])
        self.assertNotEqual(a["raw_path"], b["raw_path"])

    def test_person_spec_is_rejected(self):
        spec = self._spec()
        spec["source"] = "b2b_data_api"
        with self.assertRaises(RuntimeError):
            run_lead_gen.scrape_phase(spec, workspace_id=1)


class ListOnlyTestCase(PhaseTestBase):
    def _phase1(self):
        with mock.patch.object(gm, "run_search",
                               return_value=fake_search(SCRAPED, found=6,
                                                        dropped_chains=1,
                                                        already_seen=1)):
            return run_lead_gen.scrape_phase(self._spec(), workspace_id=1)

    def test_list_only_is_free_and_writes_nothing(self):
        phase1 = self._phase1()
        with mock.patch.object(run_lead_gen.billing, "debit_leads") as debit, \
             mock.patch("seen_ledger.mark_seen") as marked:
            out = run_lead_gen.list_only(phase1, workspace_id=1)

        debit.assert_not_called()
        marked.assert_called_once()          # delivered => recorded
        self.assertEqual(out["credits_charged"], 0)
        self.assertIsNone(out["output_path"])
        self.assertEqual(flat_lead_db.get_leads(1), [])   # no DB rows
        self.assertEqual(len(out["results"]), 2)

    def test_list_only_funnel_shows_everything(self):
        out = run_lead_gen.list_only(self._phase1(), workspace_id=1)
        self.assertEqual(out["funnel"]["found"], 6)
        self.assertEqual(out["funnel"]["delivered"], 2)
        self.assertEqual(out["funnel"]["dropped_seen"], 0)  # nothing hidden


class EnrichPhaseStaleFileTestCase(PhaseTestBase):
    """The regression the run-scoped ids exist to kill: a newer decoy
    leads_enriched_*.json from another run must never be read as this run's
    output."""

    def _fake_run_step(self, run_id):
        def step(label, args):
            if label == "Enrich leads":
                out = self.tmp / f"leads_enriched_{run_id}.json"
                out.write_text(json.dumps({
                    "enriched_at": run_id, "count": 2, "skipped": 1,
                    "excluded_existing": 1, "excluded_non_us": 0,
                    "results": [dict(SCRAPED[0], score=5),
                                dict(SCRAPED[1], score=3)],
                }), encoding="utf-8")
            # Save to Excel: nothing written here.
        return step

    def test_enrich_reads_its_own_file_not_a_newer_decoy(self):
        with mock.patch.object(gm, "run_search", return_value=fake_search(SCRAPED)):
            phase1 = run_lead_gen.scrape_phase(self._spec(), workspace_id=1)

        # A DECOY from a different run, written LATER (newer mtime) and full of
        # someone else's numbers. A glob-for-latest would grab this.
        decoy = self.tmp / "leads_enriched_99999999_999999_decoy.json"
        decoy.write_text(json.dumps({
            "enriched_at": "decoy", "count": 999, "skipped": 42,
            "excluded_existing": 7, "excluded_non_us": 3,
            "results": [{"name": "WRONG CO", "score": 1}],
        }), encoding="utf-8")
        time.sleep(0.01)

        with mock.patch.object(run_lead_gen, "run_step",
                               self._fake_run_step(phase1["run_id"])), \
             mock.patch.object(run_lead_gen.billing, "debit_leads", return_value=2), \
             mock.patch("seen_ledger.mark_seen"):
            result = run_lead_gen.enrich_phase(phase1, workspace_id=1)

        # The decoy's numbers never appear.
        self.assertEqual(result["funnel"]["delivered"], 2)
        self.assertEqual(result["funnel"]["skipped_no_contact"], 1)
        self.assertEqual(result["db_inserted"], 2)
        names = {l["name"] for l in flat_lead_db.get_leads(1)}
        self.assertNotIn("WRONG CO", names)

    def test_enrich_phase_debits_delivered_leads(self):
        with mock.patch.object(gm, "run_search", return_value=fake_search(SCRAPED)):
            phase1 = run_lead_gen.scrape_phase(self._spec(), workspace_id=1)
        with mock.patch.object(run_lead_gen, "run_step",
                               self._fake_run_step(phase1["run_id"])), \
             mock.patch.object(run_lead_gen.billing, "debit_leads",
                               return_value=2) as debit, \
             mock.patch("seen_ledger.mark_seen"):
            run_lead_gen.enrich_phase(phase1, workspace_id=1)
        debit.assert_called_once()
        self.assertEqual(debit.call_args.args[1], 2)  # 2 business leads


if __name__ == "__main__":
    unittest.main()
