"""Tests for clean_website_url (tools/scrape_google_maps.py).

Businesses stick tracking junk on their Google-listing links
(?utm_source=GMB&...); the cleaner strips it at ingestion so the DB, Excel,
and lead pages carry clean, professional URLs. All offline.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from scrape_google_maps import clean_website_url  # noqa: E402


class CleanWebsiteUrlTestCase(unittest.TestCase):
    def test_gmb_utm_link_stripped_bare(self):
        dirty = ("https://www.paulypresleyrealty.com/"
                 "?utm_source=GMB&utm_medium=organic&utm_campaign=GMB")
        self.assertEqual(clean_website_url(dirty),
                         "https://www.paulypresleyrealty.com/")

    def test_other_tracking_ids_stripped(self):
        self.assertEqual(
            clean_website_url("https://x.com/?gclid=abc&fbclid=def&srsltid=ghi"),
            "https://x.com/",
        )

    def test_non_tracking_params_kept(self):
        self.assertEqual(
            clean_website_url("https://x.com/book?location=downtown&utm_source=GMB"),
            "https://x.com/book?location=downtown",
        )

    def test_fragment_kept(self):
        self.assertEqual(
            clean_website_url("https://x.com/?utm_campaign=gmb#contact"),
            "https://x.com/#contact",
        )

    def test_url_without_query_unchanged(self):
        self.assertEqual(clean_website_url("https://x.com/about"),
                         "https://x.com/about")

    def test_none_empty_and_garbage_pass_through(self):
        self.assertIsNone(clean_website_url(None))
        self.assertEqual(clean_website_url(""), "")
        self.assertEqual(clean_website_url("not a url"), "not a url")
        self.assertEqual(clean_website_url(123), 123)


if __name__ == "__main__":
    unittest.main()
