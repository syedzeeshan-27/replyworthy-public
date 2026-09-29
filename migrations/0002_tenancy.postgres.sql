-- 0002 tenancy (plans/v2/04 A+B), Postgres dialect. Postgres DBs are always
-- fresh (0001 ran moments ago), so ALTER + backfill is enough — the backfill
-- UPDATEs are no-ops on empty tables but kept so the file is also correct if
-- data was copied in between (it isn't in practice; the copy script runs
-- after all migrations).

CREATE TABLE workspaces (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'trial',
  plan TEXT NOT NULL DEFAULT 'trial',
  created_at TEXT
);

INSERT INTO workspaces (id, name, status, plan, created_at)
VALUES (1, 'My workspace', 'active', 'trial',
        to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'));
SELECT setval(pg_get_serial_sequence('workspaces', 'id'), 1);

CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  created_at TEXT
);

CREATE TABLE hard_suppression (
  email TEXT PRIMARY KEY,
  reason TEXT,
  added_at TEXT
);

INSERT INTO hard_suppression (email, reason, added_at)
  SELECT email, reason, added_at FROM suppression
  WHERE reason IN ('unsubscribed', 'complaint')
  ON CONFLICT DO NOTHING;

CREATE TABLE mailboxes (
  id SERIAL PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  address TEXT NOT NULL,
  created_at TEXT
);

ALTER TABLE leads ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
UPDATE leads SET workspace_id = 1;
ALTER TABLE leads ALTER COLUMN workspace_id SET NOT NULL;
DROP INDEX idx_leads_status;
DROP INDEX idx_leads_email;
CREATE INDEX idx_leads_status ON leads(workspace_id, status);
CREATE INDEX idx_leads_email ON leads(workspace_id, email);
CREATE UNIQUE INDEX idx_leads_ws_place ON leads(workspace_id, place_id);

ALTER TABLE campaigns ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
UPDATE campaigns SET workspace_id = 1;
ALTER TABLE campaigns ALTER COLUMN workspace_id SET NOT NULL;
CREATE INDEX idx_campaigns_workspace ON campaigns(workspace_id);

ALTER TABLE sends ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
UPDATE sends SET workspace_id = 1;
ALTER TABLE sends ALTER COLUMN workspace_id SET NOT NULL;
CREATE INDEX idx_sends_workspace ON sends(workspace_id);

DROP TABLE suppression;
CREATE TABLE suppression (
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  email TEXT NOT NULL,
  reason TEXT,
  added_at TEXT,
  PRIMARY KEY (workspace_id, email)
);

ALTER TABLE replies ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
UPDATE replies SET workspace_id = 1;
ALTER TABLE replies ALTER COLUMN workspace_id SET NOT NULL;
CREATE INDEX idx_replies_workspace ON replies(workspace_id);

ALTER TABLE notes ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
UPDATE notes SET workspace_id = 1;
ALTER TABLE notes ALTER COLUMN workspace_id SET NOT NULL;

ALTER TABLE alerts ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id);
UPDATE alerts SET workspace_id = 1;
ALTER TABLE alerts ALTER COLUMN workspace_id SET NOT NULL;
CREATE INDEX idx_alerts_workspace ON alerts(workspace_id);

UPDATE offer_profiles SET workspace_id = 1 WHERE workspace_id IS NULL;
ALTER TABLE offer_profiles ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE offer_profiles
  ADD CONSTRAINT offer_profiles_workspace_fk
  FOREIGN KEY (workspace_id) REFERENCES workspaces(id);
CREATE INDEX idx_offers_workspace ON offer_profiles(workspace_id);
