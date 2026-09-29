"""Connector #2 — people-leads from a commercial B2B data API (plans/v2/03 C2).

Turns a person LeadSpec ("founders of marketing agencies in Austin, 25") into
people-search calls against an Apollo.io / PeopleDataLabs-class contact API
and returns unified RawLead dicts (tools/sources/base.py).

Decision V2-1 (locked): person data comes from a commercial API under its
ToS — NEVER LinkedIn scraping. The linkedin_url field is display-only.

Vendor status: the owner picks a paid contact-database vendor and
opens the account (his card). Until then this module is fully built and
tested against a fake transport — ZERO live calls. The vendor-specific bits
(endpoint URL, auth header, param names) are isolated in `spec_to_params`,
`_AUTH_HEADER`, and the response probing in `_items_from` / `normalize_person`,
so finalizing the vendor touches this file only.

Config (.env only, never in code):
    B2B_DATA_API_KEY  — the account's API key (required at call time)
    B2B_DATA_API_URL  — the people-search endpoint URL (required at call time)

Every call logs how many contacts were pulled — that number is money
(credits), and v2/05 metering builds on it.

Usage (pipeline subprocess):
    python tools/sources/b2b_data_api.py --spec-file .tmp/leadspec_<ts>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# Make `config` (project root) and sibling tools (tools/) importable whether
# this file is run as a script or imported flat/packaged.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.settings import TMP_DIR, require  # noqa: E402
from sources.base import SourceConnector  # noqa: E402
from sources.google_maps import clean_website_url  # noqa: E402

PAGE_SIZE = 25          # contacts per request (typical vendor default)
MAX_429_RETRIES = 3     # attempts per page before giving up loudly
BACKOFF_BASE = 5        # seconds; sleep BACKOFF_BASE * attempt on each 429
REQUEST_TIMEOUT = 60
_AUTH_HEADER = "X-Api-Key"  # vendor-specific; finalize with the account


def spec_to_params(spec: dict, page: int, per_page: int) -> dict:
    """Map a person LeadSpec onto the API's people-search parameters.

    Only fields the spec actually has are sent — never invent geography or
    roles the user didn't ask for.
    """
    from parse_query import geo_text  # sibling tool; lazy to avoid cycles

    params: dict = {"page": page, "per_page": per_page}
    if spec.get("role"):
        params["person_titles"] = [spec["role"]]
    if spec.get("seniority"):
        params["person_seniorities"] = [spec["seniority"]]
    if spec.get("industry"):
        params["company_industries"] = [spec["industry"]]
    elif spec.get("keywords"):
        params["keywords"] = " ".join(spec["keywords"])
    location = geo_text(spec)
    if location:
        params["person_locations"] = [location]
    return params


def _items_from(payload: dict) -> list[dict]:
    """Pull the contact list out of a response body. Vendors name the array
    differently ("results", "data", "people") — probe defensively, same
    pattern as the Maps normalize()."""
    for key in ("results", "data", "people", "matches"):
        items = payload.get(key)
        if isinstance(items, list):
            return items
    return []


def _pick(item: dict, *keys):
    """Return the first non-empty value among the given keys."""
    for key in keys:
        if item.get(key):
            return item[key]
    return None


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _full_name(item: dict) -> str | None:
    name = _pick(item, "full_name", "name")
    if name:
        return name
    first = (item.get("first_name") or "").strip()
    last = (item.get("last_name") or "").strip()
    joined = f"{first} {last}".strip()
    return joined or None


def normalize_person(item: dict, spec: dict) -> dict:
    """One raw API contact → one unified RawLead dict (base.py schema).

    Key names vary across vendors, so we probe several. Emails that came
    from the API get email_source='api' — the vendor's verification is a
    claim, so the free MX check (verify_email.py) still runs downstream as a
    second gate before any enrollment.
    """
    from parse_query import geo_text  # sibling tool; lazy to avoid cycles

    email = _first(item.get("emails")) or _pick(item, "email", "work_email")
    website = clean_website_url(
        _pick(item, "company_website", "organization_website",
              "job_company_website", "website")
    )
    return {
        "entity_type": "person",
        "name": _full_name(item),
        "title": _pick(item, "title", "job_title"),
        "company": _pick(item, "company", "company_name", "organization_name",
                         "job_company_name"),
        "website": website,
        "linkedin_url": _pick(item, "linkedin_url", "linkedin"),  # display only
        "email": email,
        "email_source": "api" if email else None,
        "phone": _first(item.get("phone_numbers"))
                 or _pick(item, "phone", "work_phone", "mobile_phone"),
        "city": _pick(item, "city", "location") or geo_text(spec) or None,
        "category": spec.get("industry"),  # keeps analyze/Excel wording sane
        "source": "b2b_data_api",
        "source_id": str(item["id"]) if item.get("id") is not None else None,
        "source_raw": item,  # anything extra lives here, not in new columns
    }


def search_people(spec: dict, session=None) -> list[dict]:
    """Run one people search for `spec`. PAID — every contact pulled burns
    vendor credits (owner's no-paid-runs-without-asking rule applies).

    Pagination stops at the spec's (already capped) count even if the API
    would page further — the cap is the cost ceiling. 429s back off and
    retry a few times, then fail loudly. Auth failures (401/403) raise
    immediately — a bad key must never quietly return zero leads.
    """
    url = require("B2B_DATA_API_URL")
    key = require("B2B_DATA_API_KEY")
    session = session or requests.Session()

    count = int(spec.get("count") or 0)
    if count <= 0:
        return []

    pulled: list[dict] = []
    page = 1
    while len(pulled) < count:
        per_page = min(PAGE_SIZE, count - len(pulled))
        params = spec_to_params(spec, page, per_page)

        attempt = 0
        while True:
            resp = session.post(
                url,
                json=params,
                headers={_AUTH_HEADER: key},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:
                attempt += 1
                if attempt > MAX_429_RETRIES:
                    raise RuntimeError(
                        f"B2B data API rate limit persisted after "
                        f"{MAX_429_RETRIES} retries (page {page})."
                    )
                delay = BACKOFF_BASE * attempt
                print(f"  [b2b] rate limited, retrying in {delay}s "
                      f"(attempt {attempt}/{MAX_429_RETRIES})", file=sys.stderr)
                time.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                # Same rule as analyze_lead/classify_reply: never silently
                # degrade on auth errors — a bad key aborts the run loudly.
                raise RuntimeError(
                    f"B2B data API rejected the API key (HTTP {resp.status_code}). "
                    "Check B2B_DATA_API_KEY in .env."
                )
            resp.raise_for_status()
            break

        items = _items_from(resp.json())
        if not items:
            break  # the search is exhausted before the cap
        pulled.extend(items[: count - len(pulled)])
        if len(items) < per_page:
            break  # short page = no more results behind it
        page += 1

    # This number is money: every pulled contact burned a vendor credit.
    # v2/05 metering reads runs from this line's payload counterpart.
    print(f"  [b2b] pulled {len(pulled)} contacts (billable)")
    return pulled


def run_search(spec: dict, session=None) -> dict:
    """One full people search: API pull → normalize → in-batch dedupe.
    Returns {results, contacts_pulled} — contacts_pulled is the BILLABLE
    number (raw contacts the vendor charged for), kept separate from
    len(results) so v2/05 metering can reconcile against the vendor
    dashboard. PAID."""
    raw = search_people(spec, session=session)
    results: list[dict] = []
    seen_keys: set = set()
    for item in raw:
        lead = normalize_person(item, spec)
        # In-batch dedupe: the DB dedupes across runs, this keeps one
        # pull from delivering the same person twice.
        dedupe_key = lead.get("email") or lead.get("source_id") \
            or (lead.get("name"), lead.get("company"))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        results.append(lead)
    return {"results": results, "contacts_pulled": len(raw)}


class B2BDataAPIConnector(SourceConnector):
    """People (by role/seniority) at companies, via a commercial B2B data
    API. Widget detection and the chain filter never run for persons — they
    answer local-business questions (plan v2/03 C3)."""

    name = "b2b_data_api"

    def search(self, spec: dict) -> list[dict]:
        return run_search(spec)["results"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search a B2B data API for person leads (PAID: burns vendor credits)."
    )
    parser.add_argument(
        "--spec-file", required=True,
        help="path to a LeadSpec JSON file (written by run_lead_gen.py)",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="scopes the output filename (b2b_raw_<run-id>.json) so "
             "concurrent runs don't collide. Default: a fresh timestamp.",
    )
    args = parser.parse_args()

    spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    search = run_search(spec)
    results = search["results"]

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = TMP_DIR / f"b2b_raw_{run_id}.json"
    payload = {
        "spec": spec,
        "scraped_at": run_id,
        "count": len(results),
        # Funnel counts (same keys the Maps payload uses, so run_pipeline's
        # funnel works unchanged): "found" = what the search surfaced.
        "found": len(results),
        "dropped_chains": 0,
        "dropped_seen": 0,
        # The money metric, reconciled against the vendor dashboard (v2/05).
        "contacts_pulled": search["contacts_pulled"],
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Found {len(results)} people. Saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
