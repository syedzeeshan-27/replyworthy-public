-- 0007 Supabase Auth — Postgres twin of the SQLite file, plus row-level
-- security. Supabase serves every table in the public schema through its
-- public Data API unless RLS is on. The app connects as the table owner
-- (which RLS does not restrict), so turning it on with no policies locks the
-- public API out and changes nothing for the app. Every table a later
-- migration creates must get the same line.

ALTER TABLE users ADD COLUMN auth_user_id TEXT;
CREATE UNIQUE INDEX idx_users_auth ON users(auth_user_id);

ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE sequence_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE sends ENABLE ROW LEVEL SECURITY;
ALTER TABLE replies ENABLE ROW LEVEL SECURITY;
ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE offer_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE suppression ENABLE ROW LEVEL SECURITY;
ALTER TABLE hard_suppression ENABLE ROW LEVEL SECURITY;
ALTER TABLE mailboxes ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY;
