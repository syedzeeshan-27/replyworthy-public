"""Scrape a website to clean markdown, with a free primary + paid fallback.

Strategy (pipeline node 3 — "Scrape their website"):
  1. Try Jina Reader (https://r.jina.ai/<url>) — free, no key.
  2. If that returns nothing, too little (< MIN_LENGTH chars), or a non-200,
     fall back to Firecrawl (uses FIRECRAWL_API_KEY, costs credits).
  3. If both fail, return None.

On success the markdown is saved to .tmp/site_<domain>_<timestamp>.md.

Usage:
    python tools/scrape_website.py https://example.com
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

# Make `config` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import TMP_DIR, require  # noqa: E402

MIN_LENGTH = 200       # below this we treat the scrape as a failure
JINA_TIMEOUT = 30      # seconds
FIRECRAWL_TIMEOUT = 60000  # milliseconds (Firecrawl SDK expects ms)


def normalize_url(url: str) -> str:
    """Ensure the URL has a scheme so both Jina and Firecrawl accept it."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def domain_slug(url: str) -> str:
    """Turn a URL into a filesystem-safe domain slug for the output filename."""
    netloc = urlparse(url).netloc or url
    netloc = netloc.lower().removeprefix("www.")
    return "".join(c if c.isalnum() else "_" for c in netloc).strip("_") or "site"


def _is_usable(text: str | None) -> bool:
    return bool(text) and len(text.strip()) >= MIN_LENGTH


def fetch_jina(url: str) -> str | None:
    """Fetch markdown via Jina Reader. Returns None on non-200 / error / empty."""
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", timeout=JINA_TIMEOUT)
    except requests.RequestException as exc:
        print(f"  [jina] request failed: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"  [jina] non-200 status: {resp.status_code}", file=sys.stderr)
        return None
    return resp.text


def fetch_firecrawl(url: str) -> str | None:
    """Fetch markdown via Firecrawl. Returns None on error."""
    try:
        from firecrawl import Firecrawl  # lazy import: only needed on fallback

        client = Firecrawl(api_key=require("FIRECRAWL_API_KEY"))
        doc = client.scrape(
            url,
            formats=["markdown"],
            only_main_content=True,
            timeout=FIRECRAWL_TIMEOUT,
        )
        return getattr(doc, "markdown", None)
    except Exception as exc:  # SDK raises various exception types; treat all as failure
        print(f"  [firecrawl] failed: {exc}", file=sys.stderr)
        return None


def scrape_website(url: str) -> str | None:
    """Scrape `url` to markdown using Jina first, Firecrawl as fallback.

    Returns the markdown text (and writes it to .tmp/), or None if both fail.
    """
    url = normalize_url(url)

    print(f"Scraping {url} ...")
    text = fetch_jina(url)
    source = "jina"

    if not _is_usable(text):
        print("  Jina result unusable - falling back to Firecrawl.")
        text = fetch_firecrawl(url)
        source = "firecrawl"

    if not _is_usable(text):
        print("  Both Jina and Firecrawl failed.", file=sys.stderr)
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = TMP_DIR / f"site_{domain_slug(url)}_{timestamp}.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"  Scraped via {source} ({len(text)} chars). Saved to {out_path}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape a website to clean markdown.")
    parser.add_argument("url", help="website URL, e.g. https://example.com")
    args = parser.parse_args()

    text = scrape_website(args.url)
    return 0 if text is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
