"""Pipeline + Excel tests for the LeadSpec flow (V2 Stage 2, plan v2/02 E).

Covers, all offline with fake pipeline steps (zero subprocesses, zero paid
calls):
- CLI path: category/city/limit still produce byte-identical scraper args.
- Spec path: a parsed LeadSpec maps onto the scraper and is stamped into
  leads.search_spec in the DB.
- Connector dispatch (V2 Stage 3, plan v2/03 E): business specs run the Maps
  scraper, person specs run the B2B data API connector, unknown sources
  refuse before anything launches.
- save_to_excel adds a "Search" sheet when a spec is known, and doesn't
  when one isn't; batches with person leads gain Title/Company/LinkedIn
  columns while business-only workbooks keep the original layout.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import run_lead_gen  # noqa: E402
import parse_query  # noqa: E402
import save_to_excel  # noqa: E402
import lead_db as flat_lead_db  # noqa: E402  (run_lead_gen's import target)
import tools.lead_db as pkg_lead_db  # noqa: E402  (same file, second module object)
from sources import google_maps as gm  # noqa: E402  (in-process maps scrape)


def _fake_search(results):
    """A run_search return value with the funnel keys scrape_phase reads.
    Story: Maps returned 6; 1 chain + 1 already-seen dropped at scrape."""
    return {
        "results": list(results),
        "found": 6, "dropped_dupes": 0, "dropped_chains": 1,
        "dropped_seen": 1, "already_seen": 1, "truncated": 0, "raw": [],
    }

ENRICHED_RESULTS = [
    {"place_id": "p1", "name": "Bright Smile Dental", "score": 5,
     "website": "https://brightsmile.example"},
    {"place_id": "p2", "name": "Boise Family Dentistry", "score": 3,
     "website": "https://boisefam.example"},
]

PERSON_RESULTS = [
    {"entity_type": "person", "name": "Jane Marsh", "title": "Founder",
     "company": "Marsh Marketing", "linkedin_url": "https://www.linkedin.com/in/jane",
     "email": "jane@marshmarketing.example", "score": 4,
     "website": "https://marshmarketing.example", "source": "b2b_data_api"},
]


class PipelineSpecTestCase(unittest.TestCase):
    """run_pipeline with fake steps: no subprocess ever launches."""

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
            # The maps scrape runs in-process now; stub the paid connector.
            mock.patch.object(gm, "run_search", self._fake_run_search),
        ]
        for p in self._patches:
            p.start()
        self.step_calls: list[tuple[str, list[str]]] = []
        self.search_calls: list[tuple] = []
        self.legacy_payloads = False  # True = enriched payload w/o funnel keys

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _fake_run_search(self, category, city, limit, keywords=None,
                         filter_seen=True):
        """Stand-in for the paid Apify scrape (google_maps.run_search)."""
        self.search_calls.append((category, city, limit, keywords, filter_seen))
        return _fake_search(ENRICHED_RESULTS)

    @staticmethod
    def _arg(args: list[str], flag: str) -> str:
        return args[args.index(flag) + 1]

    def _fake_run_step(self, label: str, args: list[str]) -> None:
        """Stand-in for the subprocess steps: records the call and writes the
        run-scoped files the next step (and _finalize_run) expects."""
        self.step_calls.append((label, args))
        tmp = run_lead_gen.TMP_DIR
        if label == "Search B2B data API":
            run_id = self._arg(args, "--run-id")
            b2b = {"count": len(PERSON_RESULTS), "found": 2,
                   "dropped_dupes": 0, "dropped_chains": 0, "dropped_seen": 0,
                   "contacts_pulled": 2, "results": PERSON_RESULTS}
            (tmp / f"b2b_raw_{run_id}.json").write_text(
                json.dumps(b2b), encoding="utf-8")
        elif label == "Enrich leads":
            run_id = self._arg(args, "--run-id")
            results = (PERSON_RESULTS
                       if any(l == "Search B2B data API" for l, _ in self.step_calls)
                       else ENRICHED_RESULTS)
            enriched = {"enriched_at": run_id, "results": results}
            if not self.legacy_payloads:
                enriched.update({"count": len(results), "skipped": 1,
                                 "excluded_existing": 0 if results is PERSON_RESULTS else 1,
                                 "excluded_non_us": 0})
            (tmp / f"leads_enriched_{run_id}.json").write_text(
                json.dumps(enriched), encoding="utf-8")
        # "Save to Excel" writes nothing here — not under test in this class.

    def _run(self, *args, **kwargs) -> dict:
        with mock.patch.object(run_lead_gen, "run_step", self._fake_run_step):
            return run_lead_gen.run_pipeline(*args, **kwargs)

    def test_cli_args_map_onto_the_connector(self):
        # The maps scrape runs in-process; assert the connector params instead
        # of a subprocess argv (the guarantee is the same, one level up).
        self._run("dentist", "Austin TX", 50)
        self.assertEqual(len(self.search_calls), 1)
        category, city, limit, _kw, filter_seen = self.search_calls[0]
        self.assertEqual((category, city, limit), ("dentist", "Austin TX", 50))
        self.assertTrue(filter_seen)  # one-shot keeps fresh-leads-only
        # No scrape subprocess any more; only enrich + save shell out.
        self.assertEqual([l for l, _ in self.step_calls],
                         ["Enrich leads", "Save to Excel"])

    def test_spec_run_maps_params_and_stamps_search_spec(self):
        spec = parse_query.spec_from_args("dentists", "", 20)
        spec["geo"] = {"city": "Boise", "region": "ID", "country": None}
        spec["raw_query"] = "dentists in Boise, 20 of them"

        result = self._run(spec=spec)

        category, city, limit, _kw, _fs = self.search_calls[0]
        self.assertEqual(category, "dentists")
        self.assertEqual(city, "Boise ID")
        self.assertEqual(limit, 20)
        self.assertEqual(result["total_found"], 6)  # raw Google Maps count
        self.assertEqual(result["hot_leads"], 1)
        self.assertEqual(result["db_inserted"], 2)
        # The funnel explains every drop ("found 6, filtered 4, delivered 2").
        self.assertEqual(result["funnel"], {
            "found": 6, "dropped_dupes": 0, "dropped_chains": 1,
            "dropped_seen": 1, "skipped_no_contact": 1, "excluded_equipped": 1,
            "excluded_non_us": 0, "delivered": 2,
        })

        # The ask survives to the DB, queryable per lead.
        for lead in flat_lead_db.get_leads(1):
            stored = json.loads(lead["search_spec"])
            self.assertEqual(stored["raw_query"], "dentists in Boise, 20 of them")

        # ...and to .tmp for the Excel subprocess.
        spec_files = list(run_lead_gen.TMP_DIR.glob("leadspec_*.json"))
        self.assertEqual(len(spec_files), 1)

    def test_cli_run_stamps_hand_built_spec(self):
        self._run("dentist", "Austin TX", 50)
        stored = json.loads(flat_lead_db.get_leads(1)[0]["search_spec"])
        self.assertEqual(stored["industry"], "dentist")
        self.assertIsNone(stored["raw_query"])

    def test_person_spec_dispatches_to_b2b_connector(self):
        spec = parse_query.spec_from_args("marketing agencies", "Austin", 100)
        spec["entity_type"] = "person"
        spec["role"] = "founder"
        spec["source"] = parse_query.route_source(spec)
        spec["raw_query"] = "founders of marketing agencies in Austin"

        result = self._run(spec=spec)

        # The B2B connector ran (not the Maps scraper), fed by the spec file.
        labels = [l for l, _ in self.step_calls]
        self.assertEqual(labels, ["Search B2B data API", "Enrich leads",
                                  "Save to Excel"])
        _, args = self.step_calls[0]
        self.assertEqual(Path(args[0]).name, "b2b_data_api.py")
        self.assertIn("--spec-file", args)
        spec_file = Path(args[args.index("--spec-file") + 1])
        self.assertEqual(json.loads(spec_file.read_text(encoding="utf-8"))["role"],
                         "founder")

        self.assertIn("--run-id", args)
        self.assertEqual(result["source"], "b2b_data_api")
        self.assertEqual(result["funnel"]["found"], 2)
        self.assertEqual(result["funnel"]["delivered"], 1)

        # Person lead landed in the DB with its person fields + the spec.
        lead = flat_lead_db.get_leads(1)[0]
        self.assertEqual(lead["entity_type"], "person")
        self.assertEqual(lead["company"], "Marsh Marketing")
        stored = json.loads(lead["search_spec"])
        self.assertEqual(stored["raw_query"],
                         "founders of marketing agencies in Austin")

    def test_unknown_source_refuses_before_any_step(self):
        spec = parse_query.spec_from_args("dentists", "Boise", 10)
        spec["source"] = "instagram_scraper"  # not a thing, and never will be
        with self.assertRaises(RuntimeError):
            self._run(spec=spec)
        self.assertEqual(self.step_calls, [])  # nothing launched, nothing spent

    def test_business_spec_still_runs_the_maps_scraper(self):
        spec = parse_query.spec_from_args("dentists", "Boise", 10)
        result = self._run(spec=spec)
        self.assertEqual(len(self.search_calls), 1)  # in-process maps scrape ran
        self.assertEqual(result["source"], "google_maps")

    def test_no_spec_and_no_args_is_an_error(self):
        with self.assertRaises(ValueError):
            self._run()
        self.assertEqual(self.step_calls, [])
        self.assertEqual(self.search_calls, [])

    def test_legacy_enriched_payload_without_count_keys_still_works(self):
        # An enriched payload from before the funnel feature has no
        # count/skipped/excluded keys — delivered falls back to len(results)
        # and every drop reason reads 0 instead of crashing.
        self.legacy_payloads = True
        result = self._run("dentist", "Austin TX", 50)
        self.assertEqual(result["funnel"], {
            "found": 6, "dropped_dupes": 0, "dropped_chains": 1,
            "dropped_seen": 1, "skipped_no_contact": 0, "excluded_equipped": 0,
            "excluded_non_us": 0, "delivered": 2,  # falls back to len(results)
        })


class PreflightPerSourceTestCase(unittest.TestCase):
    """_preflight checks the keys the chosen connector actually needs."""

    def test_b2b_source_fails_fast_without_its_keys(self):
        env = {"ANTHROPIC_API_KEY": "k", "APIFY_API_TOKEN": "k",
               "B2B_DATA_API_KEY": "", "B2B_DATA_API_URL": ""}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(RuntimeError):
                run_lead_gen._preflight("b2b_data_api")

    def test_b2b_source_needs_no_apify_token(self):
        env = {"ANTHROPIC_API_KEY": "k", "APIFY_API_TOKEN": "",
               "B2B_DATA_API_KEY": "k", "B2B_DATA_API_URL": "https://x.example",
               "FIRECRAWL_API_KEY": "k"}
        with mock.patch.dict(os.environ, env):
            run_lead_gen._preflight("b2b_data_api")  # must not raise

    def test_maps_source_still_needs_apify(self):
        env = {"ANTHROPIC_API_KEY": "k", "APIFY_API_TOKEN": ""}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(RuntimeError):
                run_lead_gen._preflight("google_maps")

    def test_scrape_only_preflight_does_not_need_anthropic(self):
        # The list fork produces name/phone/website with no Claude call, so
        # need_ai=False must not demand ANTHROPIC_API_KEY.
        env = {"ANTHROPIC_API_KEY": "", "APIFY_API_TOKEN": "k"}
        with mock.patch.dict(os.environ, env):
            run_lead_gen._preflight("google_maps", need_ai=False)  # must not raise


class ExcelSearchSheetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(save_to_excel, "OUTPUTS_DIR",
                                        Path(self._tmp.name))
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_search_sheet_written_when_spec_known(self):
        from openpyxl import load_workbook

        spec = parse_query.spec_from_args("dentists", "Boise ID", 20)
        spec["raw_query"] = "dentists in Boise, 20 of them"
        path = save_to_excel.save_to_excel(ENRICHED_RESULTS, spec=spec)
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["Leads", "Search"])
        ws = wb["Search"]
        self.assertEqual(ws["B1"].value, "dentists in Boise, 20 of them")
        self.assertEqual(ws["B2"].value, parse_query.describe_spec(spec))

    def test_no_spec_keeps_single_sheet(self):
        from openpyxl import load_workbook

        path = save_to_excel.save_to_excel(ENRICHED_RESULTS)
        self.assertEqual(load_workbook(path).sheetnames, ["Leads"])

    def test_business_only_workbook_keeps_original_columns(self):
        from openpyxl import load_workbook

        path = save_to_excel.save_to_excel(ENRICHED_RESULTS)
        headers = [c.value for c in load_workbook(path)["Leads"][1]]
        self.assertEqual(headers[0], "Business Name")
        self.assertNotIn("Title", headers)
        self.assertNotIn("Company", headers)
        self.assertNotIn("LinkedIn", headers)

    def test_mixed_workbook_gains_person_columns(self):
        from openpyxl import load_workbook

        path = save_to_excel.save_to_excel(ENRICHED_RESULTS + PERSON_RESULTS)
        ws = load_workbook(path)["Leads"]
        headers = [c.value for c in ws[1]]
        self.assertEqual(headers[:4], ["Name", "Title", "Company", "LinkedIn"])

        # Sorted by score desc: Bright Smile (5), Jane (4), Boise Family (3).
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        self.assertEqual(rows[0][0], "Bright Smile Dental")
        self.assertIn(rows[0][1], (None, ""))  # business rows: person cells blank
        self.assertEqual(rows[1][0], "Jane Marsh")
        self.assertEqual(rows[1][1], "Founder")
        self.assertEqual(rows[1][2], "Marsh Marketing")
        self.assertEqual(rows[1][3], "https://www.linkedin.com/in/jane")
        # LinkedIn cell is a real clickable hyperlink.
        self.assertEqual(ws.cell(row=3, column=4).hyperlink.target,
                         "https://www.linkedin.com/in/jane")


if __name__ == "__main__":
    unittest.main()
