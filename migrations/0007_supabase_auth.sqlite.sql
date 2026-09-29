-- 0007 Supabase Auth: passwords, email confirmation and password reset now
-- live in Supabase Auth (tools/supabase_auth.py). users.auth_user_id is the
-- Supabase user id; users.password_hash stays as a column but is written
-- empty from here on.

ALTER TABLE users ADD COLUMN auth_user_id TEXT;
CREATE UNIQUE INDEX idx_users_auth ON users(auth_user_id);
