"""Lead-list hygiene for a normalized Apify result list.

Two passes, in this order — `dedupe_exact` MUST run first:

  A. `dedupe_exact` — the SAME business returned more than once by one
     scrape (multi-term fan-out hits it from several angles). Collapsed to
     one row, later copies absorbed into the winner.
  B. `filter_chains` — genuinely DIFFERENT businesses that belong to one
     brand. Two signals, both cheap and deterministic:
       1. Intra-batch dupes — same website domain, or a same brand-name
          prefix repeated BRAND_CHAIN_MIN+ times. Drops ALL occurrences
          (we don't want any branch).
       2. Manual blocklist at `config/chain_blocklist.json` — substring
          match against the normalized name, case-insensitive. Catches
          single-batch chain hits the dup detector misses.

Order matters: `filter_chains` drops every copy of a repeated name/domain,
so feeding it un-deduped output makes it delete real leads in pairs.

Runs before the seen-ledger so chain branches never get marked seen.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import PROJECT_ROOT  # noqa: E402

BLOCKLIST_PATH = PROJECT_ROOT / "config" / "chain_blocklist.json"

# How many times a two-token name prefix must repeat before we call it a
# chain. Was effectively 2, which cost real leads once searches widened —
# same-prefix practice names are common inside one city.
BRAND_CHAIN_MIN = 3

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\D+")


def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = _PUNCT_RE.sub(" ", name.lower())
    return _WS_RE.sub(" ", s).strip()


def _brand_key(name: str | None) -> str:
    """First two tokens of the normalized name — the chain brand."""
    norm = _normalize_name(name)
    if not norm:
        return ""
    return " ".join(norm.split()[:2])


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    raw = url if url.startswith(("http://", "https://")) else "https://" + url
    host = (urlparse(raw).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def _site_key(url: str | None) -> str | None:
    """Host + path, lowercased, no www, no trailing slash. Unlike `_domain`
    this keeps the path, so two locations under one corporate domain stay
    distinguishable."""
    dom = _domain(url)
    if not dom:
        return None
    raw = url if url.startswith(("http://", "https://")) else "https://" + url
    path = (urlparse(raw).path or "").rstrip("/").lower()
    return f"{dom}{path}"


def _phone_key(phone: str | None) -> str | None:
    """Digits only, so "+1 (813) 555-0100" and "8135550100" match.

    Apify returns the same number in both shapes across rows, so the leading
    NANP country code is dropped — otherwise one business keys two ways.
    """
    if not phone:
        return None
    digits = _DIGITS_RE.sub("", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def _identity(item: dict) -> tuple | None:
    """The strongest available identity for one scraped place.

    place_id wins outright when present, and deliberately shortcuts the rest:
    two branches of a chain routinely share one website and a name prefix, but
    they always carry DIFFERENT place_ids. Falling through to the website key
    would merge them here — and `filter_chains` would then never see the
    repeated domain that identifies them as a chain.

    The weaker keys exist only for rows Apify returned without a place_id.
    Returns None when there is nothing to match on (caller keeps the row).
    """
    if item.get("place_id"):
        return ("place_id", str(item["place_id"]))
    site = _site_key(item.get("website"))
    if site:
        return ("website", site)
    name = _normalize_name(item.get("name"))
    if not name:
        return None
    phone = _phone_key(item.get("phone"))
    if phone:
        return ("name_phone", name, phone)
    area = _normalize_name(item.get("area"))
    if area:
        return ("name_area", name, area)
    return None


def _absorb(winner: dict, later: dict) -> None:
    """Fill only the keys the winner left blank. A duplicate row often carries
    a field the first copy missed (an email, a phone) — dropping it whole
    would throw that away."""
    for key, value in later.items():
        if value in (None, "", [], {}):
            continue
        if winner.get(key) in (None, "", [], {}):
            winner[key] = value


def dedupe_exact(items: list[dict]) -> tuple[list[dict], int]:
    """Collapse the SAME business appearing more than once in one scrape.

    Multi-term search fan-out returns popular places from several angles, so
    duplicates are the norm, not the exception. First occurrence wins (order
    preserved) and absorbs later copies field-wise.

    Returns (kept, n_dropped).
    """
    kept: list[dict] = []
    by_identity: dict[tuple, dict] = {}
    dropped = 0

    for item in items:
        identity = _identity(item)
        if identity is None:
            kept.append(item)  # nothing to match on — never silently drop it
            continue
        winner = by_identity.get(identity)
        if winner is None:
            by_identity[identity] = item
            kept.append(item)
        else:
            _absorb(winner, item)
            dropped += 1

    return kept, dropped


def _load_blocklist() -> list[str]:
    if not BLOCKLIST_PATH.exists():
        return []
    data = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
    return [str(n).lower().strip() for n in data.get("names", []) if n]


def filter_chains(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (kept, dropped). `dropped` preserves order for logging."""
    blocklist = _load_blocklist()

    brand_counts = Counter(_brand_key(it.get("name")) for it in items if it.get("name"))
    domain_counts = Counter(d for d in (_domain(it.get("website")) for it in items) if d)

    kept: list[dict] = []
    dropped: list[dict] = []
    for it in items:
        name = it.get("name")
        norm = _normalize_name(name)
        brand = _brand_key(name)
        dom = _domain(it.get("website"))

        # Domain repeats on 2 are decisive — same site, different place, is a
        # branch. A repeated two-token NAME prefix is much weaker evidence:
        # in a dense metro "Tampa Dental Care" and "Tampa Dental Group" are
        # unrelated practices. Two is a coincidence, BRAND_CHAIN_MIN is a chain.
        is_intra_dup = (
            (brand and brand_counts[brand] >= BRAND_CHAIN_MIN)
            or (dom and domain_counts[dom] > 1)
        )
        is_blocklisted = any(b in norm for b in blocklist) if norm else False

        if is_intra_dup or is_blocklisted:
            dropped.append(it)
        else:
            kept.append(it)
    return kept, dropped
