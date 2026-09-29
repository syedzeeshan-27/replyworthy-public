-- 0003 billing + metering (plans/v2/05 A+B), Postgres dialect. Same shape as
-- the SQLite file; timestamps stay TEXT UTC per the cross-engine rules.

CREATE TABLE credit_ledger (
  id SERIAL PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref TEXT,
  created_at TEXT
);
CREATE INDEX idx_ledger_workspace ON credit_ledger(workspace_id, id);

CREATE TABLE billing_events (
  id SERIAL PRIMARY KEY,
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
  SELECT id, 25, 'trial_grant', 'migration-0003',
         to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
  FROM workspaces WHERE id != 1 AND plan = 'trial';
UPDATE workspaces
  SET credits_balance = 25,
      trial_expires_at = to_char((now() AT TIME ZONE 'utc') + interval '14 days',
                                 'YYYY-MM-DD HH24:MI:SS')
  WHERE id != 1 AND plan = 'trial';
