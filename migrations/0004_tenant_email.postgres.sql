-- 0004 tenant email (plans/v2/06 A+B) — Postgres twin of the SQLite file.

DROP TABLE mailboxes;
CREATE TABLE mailboxes (
  id SERIAL PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  address TEXT NOT NULL,
  smtp_host TEXT,
  smtp_port INTEGER,
  imap_host TEXT,
  imap_port INTEGER,
  creds_encrypted TEXT NOT NULL,
  daily_cap INTEGER NOT NULL DEFAULT 20,
  warmup_started_at TEXT,
  status TEXT NOT NULL DEFAULT 'unverified',
  last_error TEXT,
  created_at TEXT
);
CREATE INDEX idx_mailboxes_workspace ON mailboxes(workspace_id, id);

ALTER TABLE workspaces ADD COLUMN live_sending INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN warmup_ack INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN outreach_paused INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN postal_address TEXT;
ALTER TABLE workspaces ADD COLUMN alert_email TEXT;

UPDATE workspaces SET live_sending = 1, warmup_ack = 1 WHERE id = 1;
