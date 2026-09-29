"""Extract a contact email for a lead from already-scraped site content.

Stage-1 email capture (plans/01_STAGE1_LEAD_ENGINE.md B1). Apify's contact
scraping only sometimes returns an email; without one, the outreach stage has
nothing to send to. This tool mines the website markdown that
tools/scrape_website.py already fetched (no re-scrape of the same page), and
only if that yields nothing fetches ONE extra page (contact/about/book link).

Order of preference for the address itself: a person-looking mailbox beats
office@/info@/hello@-style role addresses, which beat bare contact@. Obvious
junk (noreply@, image filenames, platform/example domains) is rejected.

Usage (CLI, for testing one site):
    python tools/extract_email.py --file .tmp/site_x.md --website https://example.com
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Sibling tool import (contact-page fallback fetch).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_website import normalize_url, scrape_website  # noqa: E402

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\s\"'<>)?]+)", re.IGNORECASE)
# Cloudflare email obfuscation: <span data-cfemail="hex..."> (XOR-encoded).
CFEMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
# Markdown links: [text](href)
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)")

# Domains that are never a business's real inbox.
JUNK_DOMAIN_PARTS = (
    "example.", "sentry.", "wixpress.com", "wix.com", "godaddy.com",
    "squarespace.com", "yourdomain.", "mysite.", "domain.com", "email.com",
    "sitename.", "company.com", "test.com", "2x.png",
)
# File-extension "domains" from image srcsets the regex can catch (foo@2x.png).
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")
# Local parts that mean "not a human inbox".
JUNK_LOCALS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "abuse", "privacy", "dmca", "unsubscribe", "bounce",
    "notifications", "alerts", "webmaster",
)
# Role addresses a small business actually reads, best first. Anything not in
# this list and not junk is treated as person-looking (ranked above all these).
ROLE_ORDER = (
    "office", "frontdesk", "reception", "appointments", "info", "hello",
    "team", "care", "smile", "admin", "contact", "support", "sales",
)

CONTACT_LINK_WORDS = ("contact", "about", "book", "appointment")


def _decode_cfemail(hexstr: str) -> str | None:
    """Decode Cloudflare's data-cfemail XOR obfuscation."""
    try:
        raw = bytes.fromhex(hexstr)
        key = raw[0]
        return "".join(chr(b ^ key) for b in raw[1:])
    except (ValueError, IndexError):
        return None


def _clean(email: str) -> str:
    return email.strip().strip(".,;:!?)('\"<>").lower()


def _is_junk(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or len(email) > 100:
        return True
    if domain.endswith(IMAGE_EXTS):
        return True
    if any(part in domain for part in JUNK_DOMAIN_PARTS):
        return True
    if any(local == j or local.startswith(j) for j in JUNK_LOCALS):
        return True
    return False


def _site_domain(website: str | None) -> str | None:
    if not website:
        return None
    netloc = urlparse(normalize_url(website)).netloc.lower()
    return netloc.removeprefix("www.") or None


def _rank(email: str, site_domain: str | None) -> tuple:
    """Sort key: lower is better. Person-looking beats role@, role order per
    ROLE_ORDER, and an address on the business's own domain beats gmail etc."""
    local, _, domain = email.partition("@")
    if local in ROLE_ORDER:
        role_rank = 1 + ROLE_ORDER.index(local)
    else:
        role_rank = 0  # person-looking
    on_site_domain = 0 if (site_domain and domain == site_domain) else 1
    return (role_rank, on_site_domain, len(email))


def extract_emails(text: str) -> list[str]:
    """All plausible emails in `text` (markdown or HTML), best candidates kept,
    junk filtered, mailto-sourced ones first, de-duplicated preserving order."""
    if not text:
        return []
    found: list[str] = []
    for m in MAILTO_RE.finditer(text):
        found.append(_clean(m.group(1).split("?")[0]))
    for m in CFEMAIL_RE.finditer(text):
        decoded = _decode_cfemail(m.group(1))
        if decoded:
            found.append(_clean(decoded))
    for m in EMAIL_RE.finditer(text):
        found.append(_clean(m.group(0)))

    out: list[str] = []
    for email in found:
        if email and not _is_junk(email) and EMAIL_RE.fullmatch(email) and email not in out:
            out.append(email)
    return out


def find_contact_page(markdown: str, website: str) -> str | None:
    """URL of the site's contact/about/booking page, if the homepage links one."""
    base = normalize_url(website)
    site_dom = _site_domain(website)
    best: str | None = None
    for text, href in MD_LINK_RE.findall(markdown or ""):
        hay = f"{text} {href}".lower()
        if not any(w in hay for w in CONTACT_LINK_WORDS):
            continue
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        url = urljoin(base + "/", href)
        if not url.startswith(("http://", "https://")):
            continue
        # Stay on the business's own site.
        if site_dom and _site_domain(url) != site_dom:
            continue
        # 'contact' beats 'about'/'book' — return immediately on the best word.
        if "contact" in hay:
            return url
        best = best or url
    return best


def ensure_email(lead: dict, markdown: str | None, website: str | None) -> tuple[str | None, str]:
    """Best contact email for `lead` and where it came from.

    Returns (email, source); source is one of
    "apify" | "mailto" | "regex" | "contact_page" | "none".
    Fetches at most one extra page (the contact page), and only when the
    homepage content yielded nothing.
    """
    site_dom = _site_domain(website)

    apify_email = _clean(lead.get("email") or "")
    if apify_email and EMAIL_RE.fullmatch(apify_email) and not _is_junk(apify_email):
        return apify_email, "apify"

    candidates = extract_emails(markdown or "")
    if candidates:
        best = min(candidates, key=lambda e: _rank(e, site_dom))
        mailto_hits = {_clean(m.group(1).split("?")[0]) for m in MAILTO_RE.finditer(markdown or "")}
        return best, ("mailto" if best in mailto_hits else "regex")

    if markdown and website:
        contact_url = find_contact_page(markdown, website)
        if contact_url:
            page = scrape_website(contact_url)
            candidates = extract_emails(page or "")
            if candidates:
                return min(candidates, key=lambda e: _rank(e, site_dom)), "contact_page"

    return None, "none"


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a contact email from scraped site content.")
    parser.add_argument("--file", required=True, help="path to scraped markdown/HTML")
    parser.add_argument("--website", help="the site's URL (for contact-page fallback + ranking)")
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    email, source = ensure_email({}, text, args.website)
    print(f"email : {email or '(none found)'}")
    print(f"source: {source}")
    return 0 if email else 1


if __name__ == "__main__":
    raise SystemExit(main())
