"""Reject tool/brand names that leak into generated outreach copy.

Platform rule: a generated cold email must never name a specific scheduling,
chat, or CRM product (e.g. "Calendly", "cal.com") — the copy should say "a
booking link" and describe the capability in plain language instead. Until now
that rule was only asserted in the prompt. Customers will soon write their own
offer text, so the rule must also be enforced in code, on the OUTPUT, after
generation.

This module holds the blocklist and the matcher. tools/analyze_lead.py runs
check_copy() on each freshly generated subject+body; on a hit it regenerates
once and, if the copy is still dirty, flags the lead for human review.

Matching is whole-word and case-insensitive, with word boundaries at both ends,
so "Calendly," (trailing punctuation) is caught but "calendlyish" and the
"cal.com" inside "local.command" are not — legitimate copy is left alone.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Make `config` importable when this module is imported in any context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import COPY_BLOCKLIST  # noqa: E402

# Common scheduling / chat / CRM tool and brand names likely to leak into
# generated copy. Lowercase; matched whole-word, case-insensitive. Multi-word
# and dotted entries ("cal.com", "jane app") match as whole phrases.
DEFAULT_BLOCKLIST = [
    "calendly",
    "cal.com",
    "acuity",
    "squarespace scheduling",
    "setmore",
    "calendso",
    "booksy",
    "vagaro",
    "mindbody",
    "jane app",
    "zocdoc",
    "podium",
    "birdeye",
    "weave",
    "intercom",
    "drift",
    "tidio",
    "tawk.to",
    "livechat",
    "zendesk",
    "hubspot",
    "gohighlevel",
    "highlevel",
    "twilio",
    "zapier",
]


def _blocklist() -> list[str]:
    """DEFAULT_BLOCKLIST plus any COPY_BLOCKLIST env extension (comma-separated).

    Read at call time so an env override picked up after import still applies.
    """
    terms = list(DEFAULT_BLOCKLIST)
    if COPY_BLOCKLIST:
        terms.extend(t.strip().lower() for t in COPY_BLOCKLIST.split(",") if t.strip())
    return terms


def find_blocked_terms(text: str) -> list[str]:
    """Return the blocklist terms that appear in `text`, in order of appearance.

    Whole-word, case-insensitive. Boundaries are `(?<![\\w])term(?![\\w])`, so a
    dotted/multi-word term matches only as a whole phrase: "cal.com" does not
    match inside "local.command", "calendly" does not match "calendlyish", but
    "Calendly," (trailing punctuation) does. Each matched term is returned once.
    """
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for term in _blocklist():
        pattern = r"(?<![\w])" + re.escape(term) + r"(?![\w])"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            hits.append((match.start(), term))
    hits.sort(key=lambda h: h[0])
    return [term for _pos, term in hits]


# Em/en dashes between digits are ranges — join them with a plain hyphen.
_DIGIT_RANGE_RE = re.compile(r"(?<=\d)[ \t]*[—–][ \t]*(?=\d)")
# Every other em/en dash (with surrounding spaces/tabs) reads best as a comma.
# [ \t] instead of \s so a dash next to a newline never merges two lines.
_DASH_RE = re.compile(r"[ \t]*[—–]+[ \t]*")
_DOUBLED_COMMA_RE = re.compile(r",[ \t]*,+")


def strip_dashes(text: str | None) -> str | None:
    """Replace em/en dashes with human punctuation (owner copy rule: zero
    em dashes in outreach copy, ever).

    Digit ranges ("8–17") become hyphenated ("8-17"); every other dash
    becomes a comma. Doubled commas, commas opening a line, and trailing
    spaces the substitution can create are cleaned up. None/empty pass
    through unchanged.
    """
    if not text:
        return text
    out = _DIGIT_RANGE_RE.sub("-", text)
    out = _DASH_RE.sub(", ", out)
    out = _DOUBLED_COMMA_RE.sub(",", out)
    out = re.sub(r"(^|\n)[ \t]*,[ \t]*", r"\1", out)  # a dash that opened a line
    out = re.sub(r"[ \t]+(?=\n|$)", "", out)          # trailing space before newline/end
    return out


def check_copy(subject: str, body: str) -> list[str]:
    """Return the union of blocked terms found in the subject and body.

    Subject matches first, then body; each term appears once.
    """
    ordered = find_blocked_terms(subject or "") + find_blocked_terms(body or "")
    result: list[str] = []
    for term in ordered:
        if term not in result:
            result.append(term)
    return result
