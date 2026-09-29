"""Detect whether a business website already has chat/booking infrastructure.

Pre-filter (runs before scrape + Claude analysis): a business that already has a
live-chat widget, click-to-WhatsApp, or an online booking system is a poor
prospect for chat/booking-style offers, so we exclude it early — before
spending Jina/Firecrawl and Claude credits on it.

Why a raw-HTML scan (not the Jina markdown): Jina returns cleaned main-content
markdown that strips JS-injected widgets (the floating WhatsApp button, chat
bubbles, booking popups). But the `<script src="...intercom...">` /
`<a href="https://wa.me/...">` tags that load those widgets live in the static
HTML before any JS runs — so a plain GET + substring scan catches them.

Fail open: if the site can't be fetched, we return all-False (don't drop a lead
just because its site was momentarily down — uncertain leads are still worth
pursuing).

Usage:
    python tools/detect_widgets.py https://example.com
"""
from __future__ import annotations

import argparse
import sys

import requests

# Substring signatures grouped by what they imply. Matched against lowercased
# page HTML.
#
# HARD signatures are evidence a third-party chat/booking product is actually
# installed (script/CDN hostnames, provider booking URLs, WhatsApp links) —
# any hit excludes the lead. SOFT signals are marketing copy ("book now",
# "schedule an appointment") that nearly every service-business template ships
# regardless of whether any software backs it — they never exclude, but are
# passed along to the Claude analysis as pitch context.
HARD_CHAT_SIGNATURES = (
    "wa.me/", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com/send",
    "tawk.to", "embed.tawk.to",
    "intercom.io", "widget.intercom", "intercomcdn",
    "js.driftt.com", "drift.com",
    "tidio.co", "code.tidio",
    "crisp.chat", "client.crisp",
    "livechatinc.com", "livechat.com",
    "static.zdassets.com", "zopim", "zendesk.com/embeddable",
    "wchat.freshchat.com", "freshchat",
    "js.hs-scripts.com", "js-na1.hs-scripts.com",
    "connect.facebook.net/en_us/sdk/xfbml.customerchat",
    "fb-customerchat", "fb-customer-chat",
    "kommo", "manychat", "chatway",
)

HARD_BOOKING_SIGNATURES = (
    "calendly.com", "assets.calendly",
    # "://" prefix so e.g. "medical.com" links don't match "cal.com".
    "://cal.com", "app.cal.com",
    "acuityscheduling.com", "app.acuityscheduling",
    "setmore.com", "my.setmore",
    "simplybook.me", "simplybook.it",
    "squareup.com/appointments", "square.site/book",
    "zocdoc.com",
    "podium.com", "widget.podium",
    "book.housecallpro", "getjobber.com",
    "vagaro.com", "schedulicity.com", "mindbodyonline.com",
    "opentable.com/booking", "resy.com",
)

SOFT_SIGNALS = (
    "book now", "book online", "book an appointment", "book appointment",
    "schedule online", "schedule an appointment", "request an appointment",
    "text us to book", "text us", "book a visit",
    "chatbot",
)

REQUEST_TIMEOUT = 10  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _fetch_html(url: str) -> str | None:
    """Fetch raw page HTML. Returns None on any error (fail open)."""
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
    except requests.RequestException as exc:
        print(f"  [detect] fetch failed: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"  [detect] non-200 status: {resp.status_code}", file=sys.stderr)
        return None
    return resp.text


def _found(html: str, signatures: tuple[str, ...]) -> list[str]:
    """Return the subset of `signatures` present in the (lowercased) html."""
    return [sig for sig in signatures if sig in html]


def detect_existing_solutions(url: str) -> dict:
    """Scan a site's raw HTML for existing chat/booking infrastructure.

    Returns {"has_chat": bool, "has_booking": bool, "detected": [str],
    "soft_signals": [str]}. Only HARD evidence (installed widget scripts,
    provider booking URLs, WhatsApp links) sets has_chat/has_booking; soft
    marketing copy is reported separately and never excludes a lead.
    Fail open: an unreachable site returns all-False so the lead is kept.
    """
    html = _fetch_html(_normalize_url(url))
    if html is None:
        return {"has_chat": False, "has_booking": False, "detected": [], "soft_signals": []}

    html_lower = html.lower()
    chat_hits = _found(html_lower, HARD_CHAT_SIGNATURES)
    booking_hits = _found(html_lower, HARD_BOOKING_SIGNATURES)
    soft_hits = _found(html_lower, SOFT_SIGNALS)

    return {
        "has_chat": bool(chat_hits),
        "has_booking": bool(booking_hits),
        "detected": chat_hits + booking_hits,
        "soft_signals": soft_hits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect existing chat/booking widgets on a website."
    )
    parser.add_argument("url", help="website URL, e.g. https://example.com")
    args = parser.parse_args()

    result = detect_existing_solutions(args.url)
    print(f"has_chat     : {result['has_chat']}")
    print(f"has_booking  : {result['has_booking']}")
    print(f"detected     : {result['detected']}")
    print(f"soft_signals : {result['soft_signals']}")
    # Exit 1 when something was detected (would be excluded), 0 otherwise.
    return 1 if (result["has_chat"] or result["has_booking"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
