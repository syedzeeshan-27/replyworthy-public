-- 0003 billing + metering (plans/v2/05 A+B): the credit ledger, the raw
-- provider-event log, and billing columns on workspaces. Workspace 1 (the
-- owner's) becomes the unmetered 'internal' plan; every other existing
-- workspace gets the same one-time trial grant new signups receive.
-- Dialect-specific SQL (datetime('now')) is allowed in migration files.

CREATE TABLE credit_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref TEXT,
  created_at TEXT
);
CREATE INDEX idx_ledger_workspace ON credit_ledger(workspace_id, id);

CREATE TABLE billing_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT,
  workspace_id INTEGER,
  raw TEXT,
  processed_at TEXT,
  created_at TEXT
);
CREATE UNIQUE INDEX idx_billing_events_id ON billing_events(provider, event_id);

ALTER TABLE workspaces ADD COLUMN credits_balance INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN frozen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN provider_customer_id TEXT;
ALTER TABLE workspaces ADD COLUMN provider_subscription_id TEXT;
ALTER TABLE workspaces ADD COLUMN trial_expires_at TEXT;

UPDATE workspaces SET plan = 'internal' WHERE id = 1;

INSERT INTO credit_ledger (workspace_id, delta, reason, ref, created_at)
  SELECT id, 25, 'trial_grant', 'migration-0003', datetime('now')
  FROM workspaces WHERE id != 1 AND plan = 'trial';
UPDATE workspaces
  SET credits_balance = 25, trial_expires_at = datetime('now', '+14 days')
  WHERE id != 1 AND plan = 'trial';
