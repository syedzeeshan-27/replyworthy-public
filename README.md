# Replyworthy

An AI sales outreach agent. It finds leads, writes each one a personal email
sequence, reads and sorts every reply, and pings a human only when one is
needed. Built as a multi-tenant web app: each customer gets a workspace, their
own mailboxes and a credit balance.

This is a portfolio copy of a private project. The code is complete and the
test suite runs, but the wording of the AI prompts has been replaced with
stand-ins, and internal planning docs are left out.

**Status:** built and tested offline. It has not been deployed and has not
sent live email.

## What it does

1. **Finds leads.** Searches Google Maps for a business type in a city,
   filters out chains and businesses that already have chat or online booking,
   and keeps their website, phone, email and social profiles.
2. **Scores and writes.** Reads each business's website and asks Claude for a
   1 to 5 fit score, the specific gaps it saw, and a personal first email. What
   is being sold is data (an "offer profile"), so the same engine pitches any
   product.
3. **Sends on a schedule.** Multi-step sequences, inside the prospect's
   business hours, under daily limits that ramp up while a new mailbox warms
   up.
4. **Reads replies.** Pulls replies over IMAP and sorts them: interested, not
   interested, unsubscribe, out of office, wrong person, or needs a human. It
   stops the sequence and queues the reply for a person. It never answers a
   prospect by itself.
5. **Protects the sender.** One-click unsubscribe, a global do-not-contact
   list, and a breaker that pauses a workspace when bounces climb.

## How it is built

| Part | Choice |
|------|--------|
| Web app | FastAPI, server-rendered Jinja pages |
| Database | Postgres on Supabase; SQLite for tests and quick local runs |
| Login | Supabase Auth (signup, email confirmation, password reset) |
| AI | Claude, with structured (JSON schema) outputs |
| Lead source | Apify Google Maps scraper; a B2B contact API for people |
| Hosting | Docker on Render |

A few things I'd point a reviewer at:

- **Tenant isolation** (`tools/lead_db.py`). Every function that touches
  customer data takes `workspace_id` as a required argument, so forgetting it
  is an error at the call site, not a data leak. `tests/test_sql_audit.py`
  reads the SQL and fails the build if a query on a tenant table has no
  workspace filter.
- **One SQL dialect for two engines** (`tools/db.py`). The same query strings
  run on SQLite and Postgres. Schema changes are plain numbered SQL files in
  `migrations/`, with no ORM.
- **Credits as a ledger** (`tools/billing.py`). Balances only change by adding
  a ledger row, a spend check runs before any paid API call, and payment
  webhooks are verified, signed and applied exactly once.
- **Encrypted mailbox logins** (`tools/mailboxes.py`). Customer mailbox
  passwords are encrypted at rest and decrypted only at the moment of sending.
- **Vendor code in one place each.** Supabase lives in
  `tools/supabase_auth.py`, the payment provider in `tools/billing.py`, the
  contact-data vendor in `tools/sources/b2b_data_api.py`.
- **An offline test suite.** About 500 tests, no network, no paid calls. The
  AI, the mail servers and the login service are replaced by stand-ins.

## Running the tests

Needs Python 3.10 or newer.

```
pip install -e .
python -m unittest discover -s tests
```

## Running the app

Copy `.env.example` to `.env` and fill in the keys it lists, then:

```
uvicorn web.app:app --reload
```

With the prompt text removed, this copy starts and the pages work, but lead
scoring and email writing will not produce useful output.

## Layout

```
run_lead_gen.py   the lead pipeline, start to finish
tools/            one Python file per job (scrape, score, send, read replies, billing)
web/              the browser app: routes, pages, background jobs
migrations/       the database shape, one numbered file per change
supabase/         SQL to create the database in Supabase
tests/            the offline test suite
landing/          the public marketing page
```

## Rights

All rights reserved. The code is published to be read. See `NOTICE`.
