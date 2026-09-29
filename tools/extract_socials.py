"""Canonicalize business social-profile links (plan: social links + DM queue).

Sibling of tools/extract_email.py. The Apify contact crawl and the already-
fetched website content both surface social URLs; this module is the ONE
place that decides what counts as a real profile/page link and what shape it
is stored in, so every source produces identical values.

Supported platforms: facebook, instagram, x (twitter), linkedin, tiktok.
Share/plugin/intent links are never profiles and are rejected.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

# host (after stripping www./m./mobile./web.) -> (platform, canonical host).
_HOST_PLATFORM = {
    "facebook.com": ("facebook", "facebook.com"),
    "fb.com": ("facebook", "facebook.com"),
    "instagram.com": ("instagram", "instagram.com"),
    "twitter.com": ("x", "x.com"),
    "x.com": ("x", "x.com"),
    "linkedin.com": ("linkedin", "linkedin.com"),
    "tiktok.com": ("tiktok", "tiktok.com"),
}
_HOST_PREFIXES = ("www.", "m.", "mobile.", "web.")

# First path segments that are platform features, never a business profile.
_FACEBOOK_REJECT = {
    "sharer", "sharer.php", "share", "share.php", "plugins", "dialog", "tr",
    "hashtag", "events", "groups", "photo", "photo.php", "watch", "login",
    "help", "policies", "privacy", "legal", "terms", "marketplace",
    "stories", "reel", "reels", "gaming", "ads", "search",
}
_INSTAGRAM_REJECT = {
    "p", "reel", "reels", "explore", "accounts", "share", "stories", "tv",
    "direct",
}
_X_REJECT = {"intent", "share", "hashtag", "search", "home", "i", "login",
             "signup"}
# LinkedIn is allowlist-shaped: everything outside these is a feature page.
_LINKEDIN_ACCEPT = {"company", "in", "school", "showcase"}


def _platform_for_host(netloc: str) -> tuple[str, str] | None:
    host = netloc.lower().rsplit("@", 1)[-1].split(":")[0]
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    if host.endswith(".linkedin.com"):  # country subdomains (uk., pk., ...)
        host = "linkedin.com"
    return _HOST_PLATFORM.get(host)


def canonical_social_url(url) -> tuple[str, str] | None:
    """Validate one URL as a business profile/page on a supported platform.

    Returns (platform, canonical_url) or None. Canonical form: https, bare
    host (twitter.com becomes x.com), no trailing slash, no query/fragment —
    except facebook's /profile.php, whose ?id= IS the profile identity.
    """
    if not url or not isinstance(url, str):
        return None
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value.lstrip("/")
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    mapped = _platform_for_host(parts.netloc)
    if not mapped:
        return None
    platform, host = mapped

    path = parts.path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None  # the platform's own homepage, not a profile
    first = segments[0].lower()

    if platform == "facebook":
        if first in _FACEBOOK_REJECT:
            return None
        if first == "profile.php":
            profile_id = dict(parse_qsl(parts.query)).get("id")
            if not profile_id:
                return None
            return platform, f"https://{host}/profile.php?id={profile_id}"
    elif platform == "instagram":
        if first in _INSTAGRAM_REJECT:
            return None
    elif platform == "x":
        if first in _X_REJECT:
            return None
    elif platform == "linkedin":
        if first not in _LINKEDIN_ACCEPT:
            return None
    elif platform == "tiktok":
        if not first.startswith("@"):
            return None

    return platform, f"https://{host}{path}"


# Any absolute URL; markdown link syntax and prose punctuation are trimmed
# after the match (a trailing ")." is sentence/markdown, not URL).
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")


def extract_social_links(markdown: str | None) -> dict:
    """All profile links in already-fetched site content, first per platform
    wins (header/footer links come first, which is where businesses put their
    real profiles)."""
    links: dict[str, str] = {}
    for match in _URL_RE.finditer(markdown or ""):
        got = canonical_social_url(match.group(0).rstrip(".,;:"))
        if got and got[0] not in links:
            links[got[0]] = got[1]
    return links


def ensure_socials(lead: dict, markdown: str | None) -> dict:
    """Best social links for `lead`: what the source connector provided,
    plus any platform it missed mined from the site content. Returns a new
    dict; the lead is never mutated."""
    links = dict(lead.get("social_links") or {})
    for platform, url in extract_social_links(markdown).items():
        links.setdefault(platform, url)
    return links
