"""Connector #1 — Google Maps business listings via Apify (plans/v2/03 B).

V2 Stage 3 refactor: the logic that lived in tools/scrape_google_maps.py
moved here, behind the SourceConnector interface, with NO behavior change.
The old module stays as a thin wrapper so existing imports and the CLI
(`python tools/scrape_google_maps.py --category ... --city ... --limit ...`)
keep working byte-for-byte.

Runs the Apify "Google Maps Scraper" actor (compass/crawler-google-places)
for a category + city, normalizes the fields we care about, then applies the
business-connector qualification steps: chain filter (filter_chains.py) and
the cross-run seen ledger (seen_ledger.py). Widget detection stays downstream
in enrich_leads.py (it needs the fetched HTML). The CLI writes results to
.tmp/maps_raw_<timestamp>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

# Make `config` (project root) and sibling tools (tools/) importable whether
# this file is run as a script or imported flat/packaged.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.settings import PROJECT_ROOT, TMP_DIR, require  # noqa: E402
from seen_ledger import load_seen, mark_seen  # noqa: E402
from extract_socials import canonical_social_url  # noqa: E402
from filter_chains import dedupe_exact, filter_chains  # noqa: E402
from sources.base import SourceConnector  # noqa: E402

ACTOR_ID = "compass~crawler-google-places"  # API form of compass/crawler-google-places
API_BASE = "https://api.apify.com/v2"
POLL_INTERVAL_SECONDS = 5
MAX_WAIT_SECONDS = 600  # generous; scraping + contact enrichment can be slow

SYNONYMS_PATH = PROJECT_ROOT / "config" / "category_synonyms.json"

# `maxCrawledPlacesPerSearch` is a ceiling PER SEARCH TERM, not a target, and
# one phrase only ever returns Google's feed for that phrase. A "limit 200"
# search used to come back with 33 because it asked one question. These three
# knobs turn the ask into several, then trim back to what was requested.
OVERSCRAPE = 1.6      # headroom before hygiene passes trim the batch
MIN_PER_TERM = 20     # a term returning <20 isn't worth fanning out for
MAX_TERMS = 5         # each term is a separate crawl — this is the cost ceiling

# Fallback shape: fold the city back into each search string. `locationQuery`
# is the better lever, but this preserves the exact phrasing that is known to
# work if a calibration run shows the bare terms under-returning.
FOLD_CITY_INTO_TERMS = False

DATASET_PAGE = 1000     # Apify's per-request item cap
MAX_DATASET_ITEMS = 5000  # backstop so a runaway run can't exhaust memory


def _load_synonyms() -> dict:
    """Optional category → extra search terms map. Absent file is fine."""
    if not SYNONYMS_PATH.exists():
        return {}
    try:
        data = json.loads(SYNONYMS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Only list values are usable; the file carries a "_comment" string, and
    # `candidates += "a string"` would append it one character at a time.
    return {k: v for k, v in data.items() if isinstance(v, list)}


def search_terms(category: str, keywords: list[str] | None = None) -> list[str]:
    """Search phrases to fan out across, most specific first.

    The category itself always leads, then the spec's own keywords, then any
    configured synonyms. Deduped case-insensitively, order preserved, capped
    at MAX_TERMS. Always returns at least one term.
    """
    candidates = [category or ""]
    candidates += list(keywords or [])
    candidates += _load_synonyms().get((category or "").strip().lower(), [])

    out: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        term = (term or "").strip()
        key = term.lower()
        if not term or key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= MAX_TERMS:
            break
    return out or [category]


def build_actor_input(category: str, city: str, limit: int,
                      keywords: list[str] | None = None) -> dict:
    """Assemble the actor input.

    The city goes in via `locationQuery` (a real actor field) rather than
    being jammed into the search text, so the actor scopes the search
    geographically instead of matching one literal phrase.
    """
    terms = search_terms(category, keywords)
    per_term = max(MIN_PER_TERM, -(-int(limit * OVERSCRAPE) // len(terms)))
    strings = [f"{t} {city}".strip() for t in terms] if FOLD_CITY_INTO_TERMS else terms
    return {
        "searchStringsArray": strings,
        "locationQuery": city,
        "maxCrawledPlacesPerSearch": per_term,
        "language": "en",
        "skipClosedPlaces": True,
        # Crawls each business website for emails + social handles (instagram, etc.).
        "scrapeContacts": True,
    }


def start_run(token: str, actor_input: dict) -> dict:
    """Kick off an async actor run. Returns the run object."""
    resp = requests.post(
        f"{API_BASE}/acts/{ACTOR_ID}/runs",
        params={"token": token},
        json=actor_input,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def wait_for_run(token: str, run_id: str) -> dict:
    """Poll the run until it reaches a terminal state or we time out."""
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        resp = requests.get(
            f"{API_BASE}/actor-runs/{run_id}",
            params={"token": token},
            timeout=30,
        )
        resp.raise_for_status()
        run = resp.json()["data"]
        status = run["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return run
        print(f"  run status: {status} ... waiting {POLL_INTERVAL_SECONDS}s")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Actor run {run_id} did not finish within {MAX_WAIT_SECONDS}s."
    )


def fetch_items(token: str, dataset_id: str,
                max_items: int | None = None) -> list[dict]:
    """Download all items from the run's default dataset, paginated.

    A single unpaginated GET silently truncates at Apify's response cap, so a
    large run would under-report its own results with no error — invisible
    while searches returned a few dozen rows, load-bearing now they don't.
    """
    ceiling = min(max_items or MAX_DATASET_ITEMS, MAX_DATASET_ITEMS)
    items: list[dict] = []
    offset = 0
    while len(items) < ceiling:
        page_size = min(DATASET_PAGE, ceiling - len(items))
        resp = requests.get(
            f"{API_BASE}/datasets/{dataset_id}/items",
            params={
                "token": token, "format": "json", "clean": "true",
                "limit": page_size, "offset": offset,
            },
            timeout=120,
        )
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list) or not page:
            break
        items.extend(page)
        if len(page) < page_size:
            break  # short page => end of dataset
        offset += len(page)
    return items


# Analytics/tracking query params that businesses stick on their Google
# listing links (utm_source=GMB etc.). Junk in a deliverable and noise for
# domain-level dedupe — stripped from every scraped website URL.
_TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "wbraid", "gbraid", "yclid",
    "mc_cid", "mc_eid", "igshid", "srsltid",
}


def clean_website_url(url):
    """Strip tracking query params (utm_* and friends) from a website URL.

    Non-tracking params are kept — some sites genuinely route on them. None,
    empty, and unparseable values pass through unchanged.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    if not parts.scheme and not parts.netloc:
        return url  # not a real URL (bare text) — leave it alone
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


def _first(value):
    """Return the first element of a list, or the value itself, or None."""
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _pick(item: dict, *keys):
    """Return the first non-empty value among the given keys."""
    for key in keys:
        if item.get(key):
            return item[key]
    return None


def _area_from(item: dict) -> str | None:
    """Best-effort locality: neighborhood > street > first comma-segment of address."""
    area = _pick(item, "neighborhood", "street")
    if area:
        return area
    address = _pick(item, "address")
    if address:
        return address.split(",")[0].strip() or None
    return None


# Actor keys per platform: the plural list first (current actor shape), then
# scalar fallbacks — same defensive probing as email/instagram above.
_SOCIAL_ITEM_KEYS = (
    ("facebooks", "facebook"),
    ("instagrams", "instagram"),
    ("twitters", "twitter"),
    ("linkedIns", "linkedins", "linkedin"),
    ("tiktoks", "tiktok"),
)


def _social_links_from(item: dict) -> dict:
    """Actor-provided social handles → {platform: canonical_url}.

    canonical_social_url owns junk rejection (share/plugin links) and the
    stored URL shape, so actor rows and site-parsed rows stay identical.
    """
    links: dict[str, str] = {}
    for keys in _SOCIAL_ITEM_KEYS:
        raw = _first(item.get(keys[0])) or _pick(item, *keys[1:])
        got = canonical_social_url(raw) if raw else None
        if got and got[0] not in links:
            links[got[0]] = got[1]
    return links


def normalize(item: dict, fallback_category: str, fallback_city: str) -> dict:
    """Extract the outreach-relevant fields from a raw Apify place item.

    Field names vary across actor versions, so we probe several keys defensively.
    """
    return {
        "place_id": _pick(item, "placeId"),
        "name": _pick(item, "title", "name"),
        "category": _pick(item, "categoryName") or _first(item.get("categories")) or fallback_category,
        "city": _pick(item, "city") or fallback_city,
        "area": _area_from(item),
        "website": clean_website_url(_pick(item, "website", "url")),
        "email": _first(item.get("emails")) or _pick(item, "email"),
        "phone": _pick(item, "phone", "phoneUnformatted"),
        "instagram": _first(item.get("instagrams")) or _pick(item, "instagram"),
        "social_links": _social_links_from(item),
        "rating": item.get("totalScore"),
        "reviews_count": item.get("reviewsCount"),
    }


def run_search(category: str, city: str, limit: int,
               keywords: list[str] | None = None,
               filter_seen: bool = True) -> dict:
    """One full Maps search: Apify scrape → normalize → dedupe → chain filter
    → seen ledger → truncate to `limit`. PAID.

    Returns {results, found, dropped_dupes, dropped_chains, dropped_seen,
    already_seen, truncated, dropped, raw} — the funnel counts downstream
    shows the owner.

    `filter_seen=False` annotates each row with `seen_before` but drops
    nothing and marks nothing. The two-phase run uses it so a preview the
    owner might cancel can't poison the ledger for later searches; it marks
    seen itself once leads are actually delivered.
    """
    token = require("APIFY_API_TOKEN")
    actor_input = build_actor_input(category, city, limit, keywords)

    print(f"Searching Google Maps for '{category}' in '{city}' (limit {limit})...")
    print(f"  search terms: {actor_input['searchStringsArray']}")
    run = start_run(token, actor_input)
    print(f"  started run {run['id']}")

    run = wait_for_run(token, run["id"])
    if run["status"] != "SUCCEEDED":
        raise RuntimeError(f"actor run ended with status {run['status']}")

    raw_items = fetch_items(token, run["defaultDatasetId"])
    all_results = [normalize(item, category, city) for item in raw_items]

    # Order is load-bearing: fanning out across search terms returns popular
    # places several times, and filter_chains drops EVERY copy of a repeated
    # name/domain — so un-deduped input would delete real leads in pairs.
    deduped, dropped_dupes = dedupe_exact(all_results)
    kept, dropped = filter_chains(deduped)

    seen = load_seen()
    for lead in kept:
        pid = lead.get("place_id")
        lead["seen_before"] = bool(pid and pid in seen)
    already_seen = sum(1 for lead in kept if lead["seen_before"])

    if filter_seen:
        results = [lead for lead in kept if not lead["seen_before"]]
        mark_seen(results, category, city)
    else:
        results = kept

    # Trim last, so leads lost to hygiene don't eat into the requested count.
    truncated = max(0, len(results) - limit)
    results = results[:limit]

    return {
        "results": results,
        "found": len(all_results),
        "dropped_dupes": dropped_dupes,
        "dropped_chains": len(dropped),
        "dropped_seen": already_seen if filter_seen else 0,
        "already_seen": already_seen,
        "truncated": truncated,
        "dropped": dropped,
        "raw": raw_items,
    }


class GoogleMapsConnector(SourceConnector):
    """Local businesses from Google Maps (via Apify). Chain filter and the
    seen ledger run inside the search — they are business-connector
    qualification steps (plan v2/03 B), not pipeline-wide ones."""

    name = "google_maps"

    def search(self, spec: dict) -> list[dict]:
        from parse_query import to_maps_params  # sibling tool; lazy to avoid cycles

        params = to_maps_params(spec)
        search = run_search(params["category"], params["city"], params["limit"],
                            keywords=spec.get("keywords"))
        results = search["results"]
        for lead in results:
            lead.setdefault("entity_type", "business")
            lead.setdefault("source", self.name)
            lead.setdefault("source_id", lead.get("place_id"))
        return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape Google Maps listings via Apify (compass/google-maps-scraper)."
    )
    parser.add_argument("--category", required=True, help='e.g. "dentist"')
    parser.add_argument("--city", required=True, help='e.g. "Austin TX"')
    parser.add_argument("--limit", type=int, default=50, help="max places to scrape (default 50)")
    args = parser.parse_args()

    try:
        search = run_search(args.category, args.city, args.limit)
    except RuntimeError as exc:
        print(f"ERROR: {exc}.", file=sys.stderr)
        return 1

    results = search["results"]
    dropped = search["dropped"]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = TMP_DIR / f"maps_raw_{timestamp}.json"
    payload = {
        "query": {"category": args.category, "city": args.city, "limit": args.limit},
        "scraped_at": timestamp,
        "count": len(results),
        # Funnel counts, so downstream can show "found X, filtered Y because Z"
        # instead of a bare total (the drop reasons used to live only in stdout).
        "found": search["found"],
        "dropped_dupes": search["dropped_dupes"],
        "dropped_chains": search["dropped_chains"],
        "dropped_seen": search["dropped_seen"],
        "already_seen": search["already_seen"],
        "truncated": search["truncated"],
        "results": results,
        "raw": search["raw"],  # keep originals so nothing is lost downstream
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"Found {search['found']} results, dropped {len(dropped)} chains, "
        f"{len(results)} new after dedup. Saved to {out_path}"
    )
    if dropped:
        names = ", ".join(d.get("name") or "(unnamed)" for d in dropped)
        print(f"  chain/dup names dropped: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
