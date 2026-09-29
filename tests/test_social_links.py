"""Tests for social-link capture (plan: social links + DM queue).

The Apify actor already crawls each business site for social handles
(`scrapeContacts: True`), but normalize() kept only `instagrams` and even that
never reached the DB or Excel. These tests pin the new behavior: every
supported platform lands in one `social_links` dict on the RawLead.

All offline — no network, no keys.

Run (from the repo root):
    python -m unittest tests.test_social_links -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from extract_socials import (  # noqa: E402
    canonical_social_url,
    ensure_socials,
    extract_social_links,
)
from sources import google_maps as gm  # noqa: E402
from sources.base import RAW_LEAD_KEYS  # noqa: E402

try:
    from tests.tenancy import TenancyTestMixin, signup_owner
except ImportError:
    from tenancy import TenancyTestMixin, signup_owner

import lead_db  # noqa: E402
import save_to_excel as sx  # noqa: E402


class NormalizeSocialLinksTestCase(unittest.TestCase):
    """normalize() maps the actor's social arrays into lead["social_links"]."""

    def test_all_supported_platforms_are_mapped(self):
        item = {
            "placeId": "p1", "title": "Tampa Smiles",
            "facebooks": ["https://www.facebook.com/tampasmiles"],
            "instagrams": ["https://www.instagram.com/tampasmiles/"],
            "twitters": ["https://x.com/tampasmiles"],
            "linkedIns": ["https://www.linkedin.com/company/tampa-smiles"],
            "tiktoks": ["https://www.tiktok.com/@tampasmiles"],
        }
        lead = gm.normalize(item, "dentist", "Tampa, FL")
        self.assertEqual(lead["social_links"], {
            "facebook": "https://facebook.com/tampasmiles",
            "instagram": "https://instagram.com/tampasmiles",
            "x": "https://x.com/tampasmiles",
            "linkedin": "https://linkedin.com/company/tampa-smiles",
            "tiktok": "https://tiktok.com/@tampasmiles",
        })

    def test_missing_socials_yield_an_empty_dict(self):
        lead = gm.normalize({"placeId": "p1", "title": "No Socials LLC"},
                            "dentist", "Tampa, FL")
        self.assertEqual(lead["social_links"], {})

    def test_singular_scalar_keys_are_probed_too(self):
        """Field names vary across actor versions (same defensive probing as
        email/instagram)."""
        item = {"placeId": "p1", "title": "T",
                "facebook": "https://facebook.com/tampasmiles",
                "twitter": "https://twitter.com/tampasmiles"}
        lead = gm.normalize(item, "dentist", "Tampa, FL")
        self.assertEqual(lead["social_links"]["facebook"],
                         "https://facebook.com/tampasmiles")
        self.assertEqual(lead["social_links"]["x"],
                         "https://x.com/tampasmiles")

    def test_flat_instagram_key_is_preserved_for_compatibility(self):
        item = {"placeId": "p1", "title": "T",
                "instagrams": ["https://www.instagram.com/tampasmiles"]}
        lead = gm.normalize(item, "dentist", "Tampa, FL")
        self.assertEqual(lead["instagram"], "https://www.instagram.com/tampasmiles")

    def test_junk_share_links_from_the_actor_are_rejected(self):
        item = {"placeId": "p1", "title": "T",
                "facebooks": ["https://www.facebook.com/sharer/sharer.php?u=x"],
                "twitters": ["https://twitter.com/intent/tweet?text=hi"]}
        lead = gm.normalize(item, "dentist", "Tampa, FL")
        self.assertEqual(lead["social_links"], {})

    def test_social_links_is_a_raw_lead_key(self):
        self.assertIn("social_links", RAW_LEAD_KEYS)


class CanonicalSocialUrlTestCase(unittest.TestCase):
    def test_facebook_numeric_profile_keeps_its_id_param(self):
        self.assertEqual(
            canonical_social_url("https://www.facebook.com/profile.php?id=61551234&mibextid=x"),
            ("facebook", "https://facebook.com/profile.php?id=61551234"))

    def test_tracking_query_params_are_dropped(self):
        self.assertEqual(
            canonical_social_url("https://www.instagram.com/tampasmiles?igsh=abc123"),
            ("instagram", "https://instagram.com/tampasmiles"))

    def test_platform_homepages_are_not_profiles(self):
        self.assertIsNone(canonical_social_url("https://www.facebook.com/"))

    def test_unrelated_domains_are_rejected(self):
        self.assertIsNone(canonical_social_url("https://connect.facebook.net/en_US/sdk.js"))
        self.assertIsNone(canonical_social_url("https://example.com/facebook.com/foo"))

    def test_linkedin_country_subdomains_map_to_the_bare_host(self):
        self.assertEqual(
            canonical_social_url("https://uk.linkedin.com/company/tampa-smiles"),
            ("linkedin", "https://linkedin.com/company/tampa-smiles"))

    def test_instagram_post_links_are_not_profiles(self):
        self.assertIsNone(canonical_social_url("https://www.instagram.com/p/Cxyz123/"))


class ExtractSocialLinksTestCase(unittest.TestCase):
    """Mining the already-fetched website markdown (the fallback when the
    actor's contact crawl came back empty)."""

    MARKDOWN = (
        "# Tampa Smiles\n"
        "Follow us on [Facebook](https://www.facebook.com/tampasmiles) and "
        "[Instagram](https://instagram.com/tampasmiles/).\n"
        "Share: https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fx\n"
        "Tweet: https://twitter.com/intent/tweet?text=hi\n"
        "Also https://www.tiktok.com/@tampasmiles.\n"
    )

    def test_finds_profiles_in_markdown_links_and_bare_urls(self):
        links = extract_social_links(self.MARKDOWN)
        self.assertEqual(links, {
            "facebook": "https://facebook.com/tampasmiles",
            "instagram": "https://instagram.com/tampasmiles",
            "tiktok": "https://tiktok.com/@tampasmiles",
        })

    def test_share_and_intent_links_never_land(self):
        links = extract_social_links(self.MARKDOWN)
        self.assertNotIn("x", links)

    def test_first_profile_per_platform_wins(self):
        md = ("https://facebook.com/first-page then "
              "https://facebook.com/second-page")
        self.assertEqual(extract_social_links(md)["facebook"],
                         "https://facebook.com/first-page")

    def test_empty_or_none_markdown_yields_empty(self):
        self.assertEqual(extract_social_links(""), {})
        self.assertEqual(extract_social_links(None), {})


class EnrichLeadSocialsTestCase(unittest.TestCase):
    """enrich_lead() lands social_links on the enriched dict — actor links
    kept, page-mined links filling the gaps (business path only)."""

    _ANALYSIS = {"pain_points": [], "score": 3,
                 "email_subject": "s", "email_body": "b"}

    def _enrich(self, lead, markdown):
        import enrich_leads as el
        with mock.patch.object(el, "scrape_website", return_value=markdown), \
             mock.patch.object(el, "analyze_lead", return_value=dict(self._ANALYSIS)), \
             mock.patch.object(el, "verify_email", return_value="unchecked"):
            return el.enrich_lead(lead)

    def test_actor_socials_survive_enrichment(self):
        lead = {"name": "T", "website": "https://t.example",
                "social_links": {"facebook": "https://facebook.com/tampasmiles"}}
        enriched = self._enrich(lead, "no socials on the page")
        self.assertEqual(enriched["social_links"]["facebook"],
                         "https://facebook.com/tampasmiles")

    def test_page_mined_socials_fill_in_when_the_actor_had_none(self):
        lead = {"name": "T", "website": "https://t.example"}
        enriched = self._enrich(
            lead, "Follow us: https://instagram.com/tampasmiles")
        self.assertEqual(enriched["social_links"],
                         {"instagram": "https://instagram.com/tampasmiles"})


class EnsureSocialsTestCase(unittest.TestCase):
    def test_actor_provided_links_win_over_the_page(self):
        lead = {"social_links": {"facebook": "https://facebook.com/from-actor"}}
        merged = ensure_socials(lead, "https://facebook.com/from-page")
        self.assertEqual(merged["facebook"], "https://facebook.com/from-actor")

    def test_page_fills_platforms_the_actor_missed(self):
        lead = {"social_links": {"facebook": "https://facebook.com/tampasmiles"}}
        merged = ensure_socials(lead, "https://instagram.com/tampasmiles")
        self.assertEqual(merged, {
            "facebook": "https://facebook.com/tampasmiles",
            "instagram": "https://instagram.com/tampasmiles",
        })

    def test_lead_without_the_key_and_no_markdown_is_fine(self):
        self.assertEqual(ensure_socials({}, None), {})

    def test_returns_a_new_dict_never_mutating_the_lead(self):
        lead = {"social_links": {}}
        merged = ensure_socials(lead, "https://instagram.com/tampasmiles")
        self.assertEqual(lead["social_links"], {})
        self.assertEqual(merged, {"instagram": "https://instagram.com/tampasmiles"})


class ExportColumnsTestCase(unittest.TestCase):
    """Facebook/Instagram land in BOTH deliverable layouts, rendered through
    the one _cell_value path (Excel + CSV + gate preview all inherit)."""

    def test_business_columns_gain_facebook_and_instagram_after_website(self):
        keys = [k for _h, k, _w in sx.COLUMNS]
        i = keys.index("website")
        self.assertEqual(keys[i + 1:i + 3], ["facebook", "instagram"])

    def test_list_columns_gain_them_too(self):
        """The free list is scraped before any AI runs and the actor already
        returns socials — the free deliverable gets them at no cost."""
        keys = [k for _h, k, _w in sx.LIST_COLUMNS]
        i = keys.index("website")
        self.assertEqual(keys[i + 1:i + 3], ["facebook", "instagram"])

    def test_cell_value_reads_the_social_dict(self):
        lead = {"social_links": {"facebook": "https://facebook.com/tampasmiles"}}
        self.assertEqual(sx._cell_value(lead, "facebook"),
                         "https://facebook.com/tampasmiles")
        self.assertEqual(sx._cell_value(lead, "instagram"), "")

    def test_cell_value_reads_the_db_json_string_shape(self):
        lead = {"social_links": json.dumps(
            {"instagram": "https://instagram.com/tampasmiles"})}
        self.assertEqual(sx._cell_value(lead, "instagram"),
                         "https://instagram.com/tampasmiles")

    def test_cell_value_falls_back_to_the_flat_legacy_key(self):
        lead = {"instagram": "https://www.instagram.com/tampasmiles"}
        self.assertEqual(sx._cell_value(lead, "instagram"),
                         "https://www.instagram.com/tampasmiles")

    def test_workbook_makes_social_cells_real_hyperlinks(self):
        wb = sx.build_workbook([{
            "name": "Tampa Smiles", "score": 4,
            "social_links": {"facebook": "https://facebook.com/tampasmiles"},
        }])
        ws = wb["Leads"]
        keys = [k for _h, k, _w in sx.COLUMNS]
        cell = ws.cell(row=2, column=keys.index("facebook") + 1)
        self.assertEqual(cell.value, "https://facebook.com/tampasmiles")
        self.assertEqual(cell.hyperlink.target, "https://facebook.com/tampasmiles")


class SocialLinksPersistenceTestCase(TenancyTestMixin, unittest.TestCase):
    """Migration 0006 + lead_db plumbing: social_links survives the DB."""

    LINKS = {"facebook": "https://facebook.com/tampasmiles",
             "instagram": "https://instagram.com/tampasmiles"}

    def setUp(self) -> None:
        self.setup_tenancy()

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_social_links_roundtrip_through_the_db(self):
        lead_db.upsert_leads(1, [{"name": "Tampa Smiles", "place_id": "p1",
                                  "social_links": dict(self.LINKS)}])
        row = lead_db.get_leads(1)[0]
        self.assertEqual(json.loads(row["social_links"]), self.LINKS)

    def test_rescrape_with_no_socials_never_blanks_known_ones(self):
        """Mirrors the email rule: a scrape that lost a field must not erase
        a known value."""
        lead_db.upsert_leads(1, [{"name": "Tampa Smiles", "place_id": "p1",
                                  "social_links": dict(self.LINKS)}])
        lead_db.upsert_leads(1, [{"name": "Tampa Smiles", "place_id": "p1",
                                  "social_links": {}}])
        row = lead_db.get_leads(1)[0]
        self.assertEqual(json.loads(row["social_links"]), self.LINKS)

    def test_lead_without_socials_stores_null(self):
        lead_db.upsert_leads(1, [{"name": "No Socials LLC", "place_id": "p2"}])
        self.assertIsNone(lead_db.get_leads(1)[0]["social_links"])


FB = "https://facebook.com/dmonlydental"
IG = "https://instagram.com/dmonlydental"


class DmWebTestCase(TenancyTestMixin, unittest.TestCase):
    """The channel filter, the social icons, and the /dms queue."""

    def setUp(self) -> None:
        self.setup_tenancy()
        import web.app as webapp
        from fastapi.testclient import TestClient
        self.webapp = webapp
        self.client = TestClient(webapp.app)
        signup_owner(self.client)
        lead_db.upsert_leads(1, [
            {"name": "Emailed Co", "place_id": "e1", "score": 3,
             "email": "hi@emailed.example"},
            {"name": "DM Only Dental", "place_id": "d1", "score": 5,
             "social_links": {"facebook": FB, "instagram": IG}},
            {"name": "Phone Only LLC", "place_id": "p1", "score": 2,
             "phone": "813-555-0100"},
        ])
        self.ids = {l["name"]: l["id"] for l in lead_db.get_leads(1)}

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_query_leads_channel_filter(self):
        for channel, expected in (
            ("email", {"Emailed Co"}),
            ("dm", {"DM Only Dental"}),
            ("phone", {"Phone Only LLC"}),
        ):
            rows, total = lead_db.query_leads(1, channel=channel)
            self.assertEqual({r["name"] for r in rows}, expected, channel)
            self.assertEqual(total, 1, channel)

    def test_pipeline_channel_filter_shows_only_dmable(self):
        html = self.client.get("/pipeline?channel=dm&partial=1").text
        self.assertIn("DM Only Dental", html)
        self.assertNotIn("Emailed Co", html)
        self.assertNotIn("Phone Only LLC", html)

    def test_pipeline_rows_link_the_social_profiles(self):
        html = self.client.get("/pipeline?partial=1").text
        self.assertIn(FB, html)
        self.assertIn(IG, html)

    def test_dms_lists_only_dmable_leads_of_this_workspace(self):
        other_ws = lead_db.create_workspace("Other Co")
        lead_db.upsert_leads(other_ws, [
            {"name": "Zulu Foreign Dental", "place_id": "z1",
             "social_links": {"facebook": "https://facebook.com/zulu"}},
        ])
        html = self.client.get("/dms").text
        self.assertIn("DM Only Dental", html)
        self.assertNotIn("Emailed Co", html)
        self.assertNotIn("Phone Only LLC", html)
        self.assertNotIn("Zulu Foreign Dental", html)

    def test_write_route_returns_the_generated_dm(self):
        with mock.patch.object(self.webapp.write_dm, "write_dm",
                               return_value={"dm_text": "Hey team, quick q?"}) as gen:
            resp = self.client.post(
                f"/dms/{self.ids['DM Only Dental']}/write")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["dm_text"], "Hey team, quick q?")
        # The lead and an offer made it into the generation call.
        lead_arg = gen.call_args.args[0]
        self.assertEqual(lead_arg["name"], "DM Only Dental")

    def test_write_route_surfaces_failures_loudly(self):
        with mock.patch.object(self.webapp.write_dm, "write_dm",
                               side_effect=ValueError("DM came back empty")):
            resp = self.client.post(
                f"/dms/{self.ids['DM Only Dental']}/write")
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("empty", body["error"])

    def test_write_route_404s_for_another_workspaces_lead(self):
        other_ws = lead_db.create_workspace("Other Co")
        lead_db.upsert_leads(other_ws, [
            {"name": "Zulu Foreign Dental", "place_id": "z1",
             "social_links": {"facebook": "https://facebook.com/zulu"}},
        ])
        foreign_id = lead_db.get_leads(other_ws)[0]["id"]
        resp = self.client.post(f"/dms/{foreign_id}/write")
        self.assertEqual(resp.status_code, 404)

    def test_mark_sent_records_the_dm_and_leaves_the_queue(self):
        lead_id = self.ids["DM Only Dental"]
        dm_text = "Hey Tampa Smiles crew, saw the phone-only booking. Worth a look?"
        resp = self.client.post(f"/dms/{lead_id}/sent",
                                data={"platform": "facebook",
                                      "dm_text": dm_text},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

        lead = lead_db.get_lead(1, lead_id)
        self.assertEqual(lead["status"], "contacted")
        self.assertEqual(lead["status_reason"], "dm_facebook")
        notes = lead_db.get_notes(1, lead_id)
        self.assertEqual(notes[0]["author"], "dm")
        self.assertIn(dm_text, notes[0]["body"])
        self.assertNotIn("DM Only Dental", self.client.get("/dms").text)

    def test_recent_dm_openings_feed_variation(self):
        lead_id = self.ids["DM Only Dental"]
        dm_text = "Hey Tampa Smiles crew, saw the phone-only booking.\nWorth a look?"
        self.client.post(f"/dms/{lead_id}/sent",
                         data={"platform": "instagram", "dm_text": dm_text},
                         follow_redirects=False)
        self.assertEqual(
            lead_db.recent_dm_openings(1),
            ["Hey Tampa Smiles crew, saw the phone-only booking."])


if __name__ == "__main__":
    unittest.main()
