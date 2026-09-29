"""Tests for lead-list dedupe hygiene (tools/filter_chains.py).

Two passes that must stay in this order: `dedupe_exact` collapses the SAME
business returned twice by one scrape, then `filter_chains` drops genuinely
different businesses that share a brand. Reversing them makes the chain
detector delete real leads in pairs, because it drops EVERY copy of a
repeated name/domain.

All offline — no network, no keys.

Run (from the repo root):
    python -m unittest tests.test_maps_dedupe -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from filter_chains import (  # noqa: E402
    BRAND_CHAIN_MIN,
    dedupe_exact,
    filter_chains,
)


def place(name, *, pid=None, website=None, phone=None, area=None, **extra):
    """A normalized Apify row, shaped like tools/sources/google_maps.normalize."""
    row = {"name": name, "place_id": pid, "website": website,
           "phone": phone, "area": area}
    row.update(extra)
    return row


class IdentityPrecedenceTestCase(unittest.TestCase):
    """place_id → website → (name, phone) → (name, area)."""

    def test_same_place_id_collapses(self):
        kept, dropped = dedupe_exact([
            place("Tampa Smiles", pid="ChIJ_aaa", website="https://a.example"),
            place("Tampa Smiles Dental", pid="ChIJ_aaa", website="https://a.example"),
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0]["name"], "Tampa Smiles")  # first occurrence wins

    def test_distinct_place_ids_never_collapse_even_sharing_a_website(self):
        """The chain case. Two branches share a corporate site but hold
        different place_ids — merging them here would hide them from
        filter_chains, which is what actually drops chains."""
        kept, dropped = dedupe_exact([
            place("Aspen Dental Tampa", pid="ChIJ_1", website="https://aspendental.com"),
            place("Aspen Dental Brandon", pid="ChIJ_2", website="https://aspendental.com"),
        ])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)

    def test_website_key_used_when_place_id_missing(self):
        kept, dropped = dedupe_exact([
            place("Bay Dental", website="https://bay.example/"),
            place("Bay Dental Care", website="http://www.bay.example"),
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_website_path_distinguishes_locations(self):
        kept, dropped = dedupe_exact([
            place("Bay Dental North", website="https://bay.example/north"),
            place("Bay Dental South", website="https://bay.example/south"),
        ])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)

    def test_name_phone_key_when_no_place_id_or_website(self):
        kept, dropped = dedupe_exact([
            place("Gulf Coast Dental", phone="+1 (813) 555-0100"),
            place("gulf coast dental!", phone="8135550100"),
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_name_area_key_is_the_last_resort(self):
        kept, dropped = dedupe_exact([
            place("Ybor Dental", area="Ybor City"),
            place("Ybor Dental", area="Ybor City"),
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_same_name_different_area_kept(self):
        kept, _ = dedupe_exact([
            place("Ybor Dental", area="Ybor City"),
            place("Ybor Dental", area="Westshore"),
        ])
        self.assertEqual(len(kept), 2)

    def test_row_with_nothing_to_match_on_is_kept_not_dropped(self):
        kept, dropped = dedupe_exact([place(None), place(None)])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)


class AbsorptionTestCase(unittest.TestCase):
    def test_duplicate_fills_blanks_on_the_winner(self):
        kept, _ = dedupe_exact([
            place("Tampa Smiles", pid="ChIJ_a", website="https://a.example",
                  email=None, phone=None),
            place("Tampa Smiles", pid="ChIJ_a", website="https://a.example",
                  email="hi@a.example", phone="813-555-0100"),
        ])
        self.assertEqual(kept[0]["email"], "hi@a.example")
        self.assertEqual(kept[0]["phone"], "813-555-0100")

    def test_duplicate_never_overwrites_a_populated_field(self):
        kept, _ = dedupe_exact([
            place("Tampa Smiles", pid="ChIJ_a", phone="813-555-0100"),
            place("Tampa Smiles", pid="ChIJ_a", phone="813-555-9999"),
        ])
        self.assertEqual(kept[0]["phone"], "813-555-0100")

    def test_order_is_preserved(self):
        kept, _ = dedupe_exact([
            place("A", pid="1"), place("B", pid="2"),
            place("A again", pid="1"), place("C", pid="3"),
        ])
        self.assertEqual([r["name"] for r in kept], ["A", "B", "C"])


class BrandChainThresholdTestCase(unittest.TestCase):
    """A repeated two-token name prefix is weak evidence; a repeated domain
    is strong. Same-prefix practice names are common inside one city."""

    def test_two_same_prefix_names_are_kept(self):
        kept, dropped = filter_chains([
            place("Tampa Dental Care", pid="1", website="https://care.example"),
            place("Tampa Dental Group", pid="2", website="https://group.example"),
        ])
        self.assertEqual(len(kept), 2, "two same-prefix practices are a coincidence")
        self.assertEqual(dropped, [])

    def test_brand_chain_min_same_prefix_names_are_dropped(self):
        items = [
            place(f"Tampa Dental {suffix}", pid=str(i),
                  website=f"https://{suffix.lower()}.example")
            for i, suffix in enumerate(("Care", "Group", "Studio", "Partners"))
        ][:BRAND_CHAIN_MIN]
        kept, dropped = filter_chains(items)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), BRAND_CHAIN_MIN)

    def test_shared_domain_still_drops_both_copies(self):
        kept, dropped = filter_chains([
            place("Aspen Dental Tampa", pid="1", website="https://aspendental.com"),
            place("Aspen Dental Brandon", pid="2", website="https://aspendental.com"),
        ])
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 2)


class PipelineOrderTestCase(unittest.TestCase):
    """The composed dedupe → chains order on a realistic widened batch."""

    def _batch(self):
        rows = [
            place(f"Practice {i}", pid=f"ChIJ_{i}",
                  website=f"https://p{i}.example", phone=f"813555{i:04d}")
            for i in range(60)
        ]
        # Multi-term fan-out returns 20 of them a second time.
        rows += [dict(rows[i]) for i in range(20)]
        # A real 2-branch chain sharing one corporate domain.
        rows += [
            place("Aspen Dental Tampa", pid="ChIJ_chain_a",
                  website="https://aspendental.com", phone="8135559001"),
            place("Aspen Dental Brandon", pid="ChIJ_chain_b",
                  website="https://aspendental.com", phone="8135559002"),
        ]
        return rows

    def test_duplicates_removed_and_chain_still_caught(self):
        deduped, n_dupes = dedupe_exact(self._batch())
        self.assertEqual(n_dupes, 20)
        self.assertEqual(len(deduped), 62)

        kept, dropped = filter_chains(deduped)
        self.assertEqual(len(dropped), 2, "the shared-domain chain is still caught")
        self.assertEqual(len(kept), 60)

    def test_no_business_appears_twice_in_the_output(self):
        """The 'no duplicate leads' guarantee, at the file level."""
        kept, _ = filter_chains(dedupe_exact(self._batch())[0])
        for field in ("place_id", "website", "phone"):
            values = [r[field] for r in kept if r.get(field)]
            self.assertEqual(len(values), len(set(values)),
                             f"duplicate {field} survived the hygiene passes")

    def test_wrong_order_would_destroy_leads(self):
        """Guards the ordering contract: chains-then-dedupe drops real leads,
        because filter_chains removes EVERY copy of a repeated domain."""
        chains_first, _ = filter_chains(self._batch())
        correct, _ = filter_chains(dedupe_exact(self._batch())[0])
        self.assertLess(len(chains_first), len(correct))


if __name__ == "__main__":
    unittest.main()
