"""Parse a freeform lead-search query into a structured LeadSpec.

V2 Stage 2 (plans/v2/02): the search box. One Claude call turns
"find me founders of marketing agencies around Austin, ~100" into a LeadSpec
dict that drives the pipeline. THIS IS DELIBERATELY THIN (decision V2-8):
one structured-outputs call, then deterministic normalization + routing in
code. It is not an agent loop and must never become one.

The model proposes, the router disposes: `route_source()` alone decides which
connector a spec runs on. The parser prompt lists exactly which sources exist
today so the model can fill `unsupported` honestly (decision V2-9 — the UI
never silently substitutes).

Usage (CLI, for testing one query — PAID, one Claude call):
    python tools/parse_query.py --query "dentists in Boise, 20 of them"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Make `config` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import LEADGEN_MODEL, require  # noqa: E402

MODEL = LEADGEN_MODEL
MAX_TOKENS = 1024
RETRY_DELAY = 20  # seconds before the single app-level retry on 429/5xx

DEFAULT_COUNT = 50
# Hard plan cap for one run — per-tier limits arrive with billing (v2/05).
# Applied in code BEFORE any job starts; the UI says so when it trims an ask.
MAX_LEAD_COUNT = 500

# Structured-output schema: the API guarantees the response is valid JSON
# matching this shape. Unknown string fields come back as "" (the prompt says
# so) and are normalized to None in _normalize_spec — no union types needed.
LEADSPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_type": {"type": "string", "enum": ["business", "person"]},
        "source": {"type": "string", "enum": ["google_maps", "b2b_data_api"]},
        "industry": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "geo": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "region": {"type": "string"},
                "country": {"type": "string"},
            },
            "required": ["city", "region", "country"],
            "additionalProperties": False,
        },
        "role": {"type": "string"},
        "seniority": {"type": "string"},
        "count": {"type": "integer"},
        "qualification_hints": {"type": "string"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": [
        "entity_type", "source", "industry", "keywords", "geo", "role",
        "seniority", "count", "qualification_hints", "unsupported",
        "confidence",
    ],
    "additionalProperties": False,
}

# The complete list of sources that exist TODAY. Every added source must be
# added here AND to route_source() in the same commit, or `unsupported`
# starts lying (plan v2/02 D, "prompt drift").
_SYSTEM_PROMPT = (
    "You parse ONE freeform lead-search request into a structured LeadSpec "
    "for a lead-generation pipeline. You only parse — code decides what "
    "actually runs.\n\n"
    "Sources that exist TODAY (the complete list):\n"
    "- google_maps: local BUSINESSES found on Google Maps. Works best with "
    "city-level geography.\n"
    "- b2b_data_api: PEOPLE (by role/seniority) at companies, via a B2B "
    "contact database.\n"
    "Nothing else exists yet — no Instagram, no LinkedIn scraping, no "
    "review sites, no importing lists, no phone/SMS scraping. Any part of "
    "the request these two sources cannot deliver goes into \"unsupported\" "
    "as short fragments quoted from the request.\n\n"
    "Field rules:\n"
    "- entity_type: \"person\" only when the request targets individual "
    "people (founders, owners, heads of X); otherwise \"business\".\n"
    "- source: propose google_maps for businesses, b2b_data_api for people.\n"
    "- industry: a normalized business/industry noun phrase, e.g. "
    "\"marketing agencies\". \"\" if the request names no recognizable "
    "business type.\n"
    "- keywords: 1-5 short search terms for the source's search box.\n"
    "- geo: split what was stated into city/region/country (\"around "
    "Austin\" -> city \"Austin\"). Use \"\" for parts not stated; never "
    "invent geography.\n"
    "- role / seniority: for person requests only; \"\" otherwise.\n"
    "- count: the number of leads requested; 50 if not stated.\n"
    "- qualification_hints: extra fit criteria stated in the request (e.g. "
    "\"without online booking\"); \"\" if none.\n"
    "- confidence: \"low\" when the request is ambiguous or missing "
    "something the search needs (e.g. a business search with no location "
    "at all); otherwise \"high\".\n"
    "- Use \"\" (empty string) for any unknown string field, never the "
    "word \"null\"."
)


def route_source(spec: dict) -> str:
    """Deterministic router — the model proposes, this function disposes
    (plan v2/02 A). person -> b2b_data_api; everything else -> google_maps."""
    if (spec.get("entity_type") or "").strip() == "person":
        return "b2b_data_api"
    return "google_maps"


_client = None


def _get_client():
    """Lazily construct (and cache) the Anthropic client.

    Same pattern as tools/analyze_lead.py: a getter so tests can monkeypatch
    `parse_query._get_client` with a fake, no real key or network needed.
    """
    global _client
    if _client is None:
        from anthropic import Anthropic  # lazy import: only needed for live calls

        _client = Anthropic(api_key=require("ANTHROPIC_API_KEY"))
    return _client


def _call_claude(query: str) -> dict:
    """Send the parse prompt to Claude and return the raw JSON result."""
    client = _get_client()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # Sonnet 5 runs adaptive thinking when the field is omitted — disable
        # explicitly: this is a short extraction task, thinking tokens are waste.
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": LEADSPEC_SCHEMA},
        },
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Lead-search request:\n{query}"}],
    )
    if msg.stop_reason == "max_tokens":
        raise ValueError("response truncated at max_tokens")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def _call_with_retry(query: str) -> dict:
    """One app-level retry on rate-limit/5xx/network errors, on top of the
    SDK's built-in retries. Auth and other 4xx errors propagate immediately —
    there is no fallback spec, so a failed parse surfaces to the caller."""
    from anthropic import APIConnectionError, APIStatusError

    try:
        return _call_claude(query)
    except APIConnectionError:
        print(f"  [parse] connection error, retrying in {RETRY_DELAY}s", file=sys.stderr)
        time.sleep(RETRY_DELAY)
        return _call_claude(query)
    except APIStatusError as exc:
        if exc.status_code == 429 or exc.status_code >= 500:
            print(
                f"  [parse] transient {exc.status_code}, retrying in {RETRY_DELAY}s",
                file=sys.stderr,
            )
            time.sleep(RETRY_DELAY)
            return _call_claude(query)
        raise


def _clean(value) -> str | None:
    """'' / whitespace / None -> None; anything else -> stripped str."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_spec(data: dict, raw_query: str | None) -> dict:
    """Coerce a parse result into a safe LeadSpec.

    Everything security- or money-relevant happens HERE, in code, not in the
    model: the count cap, the source routing, and type safety. Adds two
    code-owned keys on top of the model schema: `raw_query` (what the user
    typed, so "what did I ask for" survives to the DB/Excel/lead page) and
    `requested_count` (only when the ask exceeded the cap, so the UI can say
    so honestly).
    """
    entity_type = _clean(data.get("entity_type")) or "business"
    if entity_type not in ("business", "person"):
        entity_type = "business"

    keywords = data.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = [keywords]
    keywords = [k for k in (_clean(k) for k in keywords) if k]

    geo_in = data.get("geo") or {}
    if not isinstance(geo_in, dict):
        geo_in = {}
    geo = {k: _clean(geo_in.get(k)) for k in ("city", "region", "country")}

    unsupported = data.get("unsupported") or []
    if not isinstance(unsupported, list):
        unsupported = [unsupported]
    unsupported = [u for u in (_clean(u) for u in unsupported) if u]

    try:
        requested = int(data.get("count"))
    except (TypeError, ValueError):
        requested = DEFAULT_COUNT
    if requested <= 0:
        requested = DEFAULT_COUNT
    count = min(requested, MAX_LEAD_COUNT)

    confidence = _clean(data.get("confidence"))
    if confidence not in ("high", "low"):
        confidence = "low"

    spec = {
        "entity_type": entity_type,
        "industry": _clean(data.get("industry")),
        "keywords": keywords,
        "geo": geo,
        "role": _clean(data.get("role")),
        "seniority": _clean(data.get("seniority")),
        "count": count,
        "qualification_hints": _clean(data.get("qualification_hints")),
        "unsupported": unsupported,
        "confidence": confidence,
        "raw_query": raw_query,
    }
    # The router, not the model, picks the source (plan v2/02 A).
    spec["source"] = route_source(spec)
    if requested > MAX_LEAD_COUNT:
        spec["requested_count"] = requested
    return spec


def parse_query(query: str) -> dict:
    """Parse one freeform query into a normalized LeadSpec. PAID (one Claude
    call, fractions of a cent). Raises ValueError on a blank query; API
    errors (including auth) propagate — there is no junk fallback."""
    query = (query or "").strip()
    if not query:
        raise ValueError("empty query")
    return _normalize_spec(_call_with_retry(query), raw_query=query)


def spec_from_args(category: str, city: str, limit: int = DEFAULT_COUNT,
                   entity_type: str = "business") -> dict:
    """Hand-built LeadSpec for the search form and the CLI — free, instant,
    no Claude call (plan v2/02 C). `entity_type` picks who is being looked
    for; the source follows from it via route_source()."""
    return _normalize_spec(
        {
            "entity_type": entity_type,
            "industry": category,
            "keywords": [category] if category else [],
            "geo": {"city": city, "region": "", "country": ""},
            "role": "",
            "seniority": "",
            "count": limit,
            "qualification_hints": "",
            "unsupported": [],
            "confidence": "high",
        },
        raw_query=None,
    )


def geo_text(spec: dict) -> str:
    """One display/search string for the spec's location, most specific
    first. Geo stays structured in the spec (plan v2/02 D) — this is one
    connector-side flattening, not a replacement for the object."""
    geo = spec.get("geo") or {}
    parts = [geo.get("city"), geo.get("region")]
    joined = " ".join(p for p in parts if p)
    return joined or (geo.get("country") or "")


def to_maps_params(spec: dict) -> dict:
    """Map a business LeadSpec onto the Google Maps connector's parameters
    ({category, city, limit} — exactly what tools/scrape_google_maps.py
    takes)."""
    category = spec.get("industry") or " ".join(spec.get("keywords") or [])
    return {"category": category, "city": geo_text(spec), "limit": spec["count"]}


def describe_spec(spec: dict) -> str:
    """One human-readable line: 'dentists in Boise (max 20)'. Used by the UI
    confirm panel, the job page, and the Excel Search sheet."""
    what = spec.get("industry") or " ".join(spec.get("keywords") or []) or "leads"
    role = spec.get("role")
    if spec.get("entity_type") == "person" and role:
        what = f"{role} of {what}"
    where = geo_text(spec)
    line = what
    if where:
        line += f" in {where}"
    line += f" (max {spec.get('count', DEFAULT_COUNT)})"
    hints = spec.get("qualification_hints")
    if hints:
        line += f" — {hints}"
    return line


def spec_is_unusable(spec: dict) -> bool:
    """True when the parse found nothing searchable at all (gibberish input):
    no industry, no keywords, no role. The UI shows example queries instead
    of an error (plan v2/02 B)."""
    return not (spec.get("industry") or spec.get("keywords") or spec.get("role"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse one freeform lead-search query (PAID: one Claude call)."
    )
    parser.add_argument("--query", required=True, help="the freeform search request")
    args = parser.parse_args()

    spec = parse_query(args.query)
    print(json.dumps(spec, indent=2, ensure_ascii=False))
    print(f"\nUnderstood as: {describe_spec(spec)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
