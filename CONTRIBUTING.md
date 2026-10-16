# Contributing

Thanks for wanting to help. Issues and pull requests are both welcome.

## Getting set up

Follow the Quickstart in the README. You don't need any API keys to run the
app or the tests.

```bash
python -m unittest discover -s tests
```

The suite is offline by design: it pins `DRY_RUN=true`, uses temp SQLite
files, and swaps Supabase Auth for an in-memory fake (`tests/fake_supabase.py`).
A pull request should leave it green.

## House rules

These keep the app safe to run against real inboxes. Please keep them.

- **Secrets only in `.env`.** Never in code, comments, logs or commits.
- **`DRY_RUN` stays true by default.** Nothing sends for real unless the
  person running it turns that off.
- **The app never auto-replies to a prospect.** It classifies the reply, stops
  the sequence and hands it to a human.
- **Every tenant query carries `workspace_id`.** `tests/test_sql_audit.py`
  checks this.
- **SQL runs on SQLite and Postgres.** Schema changes are a new numbered pair
  in `migrations/` (`.sqlite.sql` + `.postgres.sql`). After adding one, run
  `python tools/build_supabase_sql.py`.
- **One module per outside service.** Supabase Auth lives only in
  `tools/supabase_auth.py`, billing only in `tools/billing.py`, the contact
  data vendor only in `tools/sources/b2b_data_api.py`.
- **Generated email never names a product.** Say "a booking link", not a
  brand. `tools/copy_guard.py` enforces it.
- **No LinkedIn scraping.** People leads come from a paid B2B data API only.

## Good first issues

- Field mapping for a specific B2B contact-data vendor
  (`tools/sources/b2b_data_api.py`).
- More reply-classification examples and tests.
- Translations of the dashboard.
