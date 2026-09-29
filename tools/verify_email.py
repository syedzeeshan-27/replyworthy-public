"""Verify a lead's email address is plausibly deliverable — free checks only.

Stage-1 verification (plans/01_STAGE1_LEAD_ENGINE.md B2): syntax + DNS.
An address passes ("mx_ok") when its domain has MX records, or — per RFC 5321
fallback delivery — an A/AAAA record. No SMTP handshake: port 25 is blocked on
home networks/Render and probing looks abusive. If bounce rates ever exceed
~5% in outreach, add a paid verification API on top (key via .env), not here.

Results:
    mx_ok       domain can receive mail — sendable
    no_mx       domain doesn't exist / can't receive mail — never send
    bad_syntax  not an email address
    unchecked   no email given, or DNS lookup itself failed (timeout etc.)

Usage:
    python tools/verify_email.py someone@example.com
"""
from __future__ import annotations

import argparse
import re

import dns.resolver
from dns.exception import DNSException

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
DNS_LIFETIME = 6.0  # seconds per lookup — a slow resolver must not stall the run

# One DNS answer per domain per run (leads often share franchise/host domains).
_domain_cache: dict[str, str] = {}


def _domain_receives_mail(domain: str) -> str:
    try:
        dns.resolver.resolve(domain, "MX", lifetime=DNS_LIFETIME)
        return "mx_ok"
    except dns.resolver.NXDOMAIN:
        return "no_mx"
    except dns.resolver.NoAnswer:
        # No MX record: RFC 5321 falls back to the A/AAAA record.
        for rtype in ("A", "AAAA"):
            try:
                dns.resolver.resolve(domain, rtype, lifetime=DNS_LIFETIME)
                return "mx_ok"
            except dns.resolver.NXDOMAIN:
                return "no_mx"
            except DNSException:
                continue
        return "no_mx"
    except DNSException:
        # Timeout / resolver trouble — inconclusive, don't fail the lead.
        return "unchecked"


def verify_email(email: str | None) -> str:
    """Classify `email` as mx_ok | no_mx | bad_syntax | unchecked."""
    if not email:
        return "unchecked"
    email = email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return "bad_syntax"
    domain = email.rsplit("@", 1)[1]
    if domain not in _domain_cache:
        _domain_cache[domain] = _domain_receives_mail(domain)
    return _domain_cache[domain]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an email's deliverability (syntax + DNS).")
    parser.add_argument("email")
    args = parser.parse_args()

    result = verify_email(args.email)
    print(result)
    return 0 if result == "mx_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
