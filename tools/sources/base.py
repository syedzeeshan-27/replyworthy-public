"""Connector interface + the unified RawLead schema (plans/v2/03 A).

Every lead source implements `SourceConnector.search(spec) -> list[RawLead]`.
A RawLead is a plain dict (matching how the rest of the pipeline passes leads
around) with ONE schema for both shapes — a superset of the original Google
Maps lead dict, so lead_db upserts and the Excel export extend rather than
fork:

| Field           | Business (Maps)          | Person (B2B data API)          |
|-----------------|--------------------------|--------------------------------|
| entity_type     | "business"               | "person"                       |
| name            | business name            | full name                      |
| company         | = name                   | company name                   |
| title           | —                        | job title                      |
| website         | business site            | company site                   |
| linkedin_url    | —                        | profile URL (display only —    |
|                 |                          | never scraped, decision V2-1)  |
| email           | extracted later          | usually included by the API    |
| email_source    | mailto/regex/... (later) | "api"                          |
| phone/address/  | from Maps                | company HQ if provided         |
|   city          |                          |                                |
| source          | "google_maps"            | "b2b_data_api"                 |
| source_id       | Apify place id           | the API's person id            |
| source_raw      | —                        | anything extra the API returned|

Schema drift rule (plan v2/03 D): resist per-source columns beyond this —
`source_raw` (JSON) holds anything extra. This schema is the contract that
keeps Stages 2–5 source-agnostic.
"""
from __future__ import annotations

# The unified schema's keys, for reference and tests. Business leads may omit
# the person-only keys and vice versa; consumers use .get() throughout.
RAW_LEAD_KEYS = (
    "entity_type", "name", "company", "title", "website", "linkedin_url",
    "email", "email_source", "phone", "address", "city", "area",
    "source", "source_id", "source_raw",
    # business-only extras that predate the schema (kept for compatibility)
    "place_id", "category", "instagram", "rating", "reviews_count",
    # cross-source social profiles, {"facebook"|"instagram"|"x"|"linkedin"|
    # "tiktok": canonical url} — shaped by tools/extract_socials.py
    "social_links",
)


class SourceConnector:
    """One lead source. Implementations map a LeadSpec (tools/parse_query.py)
    onto their API and return unified RawLead dicts."""

    name: str = "base"

    def search(self, spec: dict) -> list[dict]:
        """Run one search for `spec` and return RawLead dicts. PAID on every
        real connector — callers own the owner's no-paid-runs-without-asking
        rule."""
        raise NotImplementedError
