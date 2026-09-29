-- 0004 tenant email (plans/v2/06 A+B): the real mailboxes table (the 0002
-- stub was a reservation — nothing ever wrote a row to it), plus the
-- per-workspace sending switches and CAN-SPAM identity.
--
-- creds_encrypted holds a Fernet token (MAILBOX_ENC_KEY in .env) wrapping
-- the SMTP/IMAP login as JSON. TEXT on both engines: Fernet tokens are
-- URL-safe base64 ASCII.

DROP TABLE mailboxes;
CREATE TABLE mailboxes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- Per-workspace sending mode (plans/v2/06 B): every workspace starts in
-- practice mode (live_sending=0); the customer flips it behind the go-live
-- checklist. The global DRY_RUN env stays as the platform master override.
ALTER TABLE workspaces ADD COLUMN live_sending INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN warmup_ack INTEGER NOT NULL DEFAULT 0;
-- Per-workspace pause: the bounce circuit breaker trips ONE workspace now
-- (plans/v2/06 C2); the PAUSED file stays the global admin kill switch.
ALTER TABLE workspaces ADD COLUMN outreach_paused INTEGER NOT NULL DEFAULT 0;
-- CAN-SPAM identity, collected at onboarding, required before going live.
ALTER TABLE workspaces ADD COLUMN postal_address TEXT;
-- Where THIS workspace's alerts go by email (mailbox broken, hot lead).
ALTER TABLE workspaces ADD COLUMN alert_email TEXT;

-- The owner's workspace keeps its V1 behavior: env-configured mailboxes and
-- the global DRY_RUN switch decide, so live_sending must not block it.
UPDATE workspaces SET live_sending = 1, warmup_ack = 1 WHERE id = 1;
