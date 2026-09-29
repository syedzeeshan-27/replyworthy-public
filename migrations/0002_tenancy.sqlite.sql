-- 0002 tenancy (plans/v2/04 A+B): workspaces, users, workspace_id NOT NULL
-- on every tenant table, per-workspace suppression + global hard_suppression,
-- reserved mailboxes table. Existing data becomes workspace 1.
--
-- SQLite cannot ALTER a column to NOT NULL, so tenant tables are rebuilt
-- (create new -> copy -> drop -> rename). Dialect-specific SQL (including
-- datetime('now')) is allowed here — migration files are per-engine.

CREATE TABLE workspaces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'trial',
  plan TEXT NOT NULL DEFAULT 'trial',
  created_at TEXT
);

INSERT INTO workspaces (id, name, status, plan, created_at)
VALUES (1, 'My workspace', 'active', 'trial', datetime('now'));

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- Unsubscribes recorded before this stage were real unsubscribes: they block
-- every workspace from now on (plans/v2/04 B, "be conservative").
INSERT INTO hard_suppression (email, reason, added_at)
  SELECT email, reason, added_at FROM suppression
  WHERE reason IN ('unsubscribed', 'complaint')
  ON CONFLICT DO NOTHING;

-- Reserved for v2/06 (tenant-owned sending mailboxes).
CREATE TABLE mailboxes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  address TEXT NOT NULL,
  created_at TEXT
);

-- ---- leads ----
CREATE TABLE leads_new (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id   INTEGER NOT NULL REFERENCES workspaces(id),
  place_id       TEXT,
  name           TEXT NOT NULL,
  category       TEXT,
  city           TEXT,
  area           TEXT,
  website        TEXT,
  phone          TEXT,
  email          TEXT,
  email_source   TEXT,
  email_verified TEXT DEFAULT 'unchecked',
  rating         REAL,
  reviews_count  INTEGER,
  score          INTEGER,
  pain_points    TEXT,
  email_subject  TEXT,
  email_body     TEXT,
  soft_signals   TEXT,
  status         TEXT DEFAULT 'new',
  status_reason  TEXT,
  first_seen_at  TEXT,
  updated_at     TEXT,
  ghl_contact_id TEXT,
  ghl_opportunity_id TEXT,
  ghl_synced_at  TEXT,
  ghl_synced_status TEXT,
  offer_id       INTEGER,
  search_spec    TEXT,
  entity_type    TEXT,
  company        TEXT,
  title          TEXT,
  linkedin_url   TEXT,
  source         TEXT,
  source_id      TEXT,
  source_raw     TEXT
);
INSERT INTO leads_new (id, workspace_id, place_id, name, category, city, area,
  website, phone, email, email_source, email_verified, rating, reviews_count,
  score, pain_points, email_subject, email_body, soft_signals, status,
  status_reason, first_seen_at, updated_at, ghl_contact_id,
  ghl_opportunity_id, ghl_synced_at, ghl_synced_status, offer_id, search_spec,
  entity_type, company, title, linkedin_url, source, source_id, source_raw)
  SELECT id, 1, place_id, name, category, city, area,
    website, phone, email, email_source, email_verified, rating, reviews_count,
    score, pain_points, email_subject, email_body, soft_signals, status,
    status_reason, first_seen_at, updated_at, ghl_contact_id,
    ghl_opportunity_id, ghl_synced_at, ghl_synced_status, offer_id, search_spec,
    entity_type, company, title, linkedin_url, source, source_id, source_raw
  FROM leads;
DROP TABLE leads;
ALTER TABLE leads_new RENAME TO leads;
CREATE INDEX idx_leads_status ON leads(workspace_id, status);
CREATE INDEX idx_leads_email ON leads(workspace_id, email);
CREATE UNIQUE INDEX idx_leads_ws_place ON leads(workspace_id, place_id);

-- ---- campaigns ----
CREATE TABLE campaigns_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  category TEXT,
  city TEXT,
  status TEXT DEFAULT 'draft',
  daily_cap INTEGER DEFAULT 30,
  created_at TEXT,
  offer_id INTEGER
);
INSERT INTO campaigns_new (id, workspace_id, name, category, city, status,
  daily_cap, created_at, offer_id)
  SELECT id, 1, name, category, city, status, daily_cap, created_at, offer_id
  FROM campaigns;
DROP TABLE campaigns;
ALTER TABLE campaigns_new RENAME TO campaigns;
CREATE INDEX idx_campaigns_workspace ON campaigns(workspace_id);

-- ---- sends ----
CREATE TABLE sends_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  step_number INTEGER NOT NULL,
  mailbox TEXT,
  message_id TEXT,
  scheduled_at TEXT NOT NULL,
  sent_at TEXT,
  status TEXT DEFAULT 'scheduled',
  error TEXT
);
INSERT INTO sends_new (id, workspace_id, lead_id, campaign_id, step_number,
  mailbox, message_id, scheduled_at, sent_at, status, error)
  SELECT id, 1, lead_id, campaign_id, step_number,
    mailbox, message_id, scheduled_at, sent_at, status, error
  FROM sends;
DROP TABLE sends;
ALTER TABLE sends_new RENAME TO sends;
CREATE INDEX idx_sends_due ON sends(status, scheduled_at);
CREATE INDEX idx_sends_lead ON sends(lead_id);
CREATE INDEX idx_sends_workspace ON sends(workspace_id);

-- ---- suppression (composite key: one list per workspace) ----
CREATE TABLE suppression_new (
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  email TEXT NOT NULL,
  reason TEXT,
  added_at TEXT,
  PRIMARY KEY (workspace_id, email)
);
INSERT INTO suppression_new (workspace_id, email, reason, added_at)
  SELECT 1, email, reason, added_at FROM suppression;
DROP TABLE suppression;
ALTER TABLE suppression_new RENAME TO suppression;

-- ---- replies ----
CREATE TABLE replies_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  lead_id INTEGER REFERENCES leads(id),
  send_id INTEGER REFERENCES sends(id),
  mailbox TEXT NOT NULL,
  from_email TEXT NOT NULL,
  subject TEXT,
  body_text TEXT,
  received_at TEXT NOT NULL,
  imap_uid TEXT,
  classification TEXT,
  confidence TEXT,
  summary TEXT,
  suggested_action TEXT,
  processed_at TEXT,
  ghl_pushed_at TEXT
);
INSERT INTO replies_new (id, workspace_id, lead_id, send_id, mailbox,
  from_email, subject, body_text, received_at, imap_uid, classification,
  confidence, summary, suggested_action, processed_at, ghl_pushed_at)
  SELECT id, 1, lead_id, send_id, mailbox,
    from_email, subject, body_text, received_at, imap_uid, classification,
    confidence, summary, suggested_action, processed_at, ghl_pushed_at
  FROM replies;
DROP TABLE replies;
ALTER TABLE replies_new RENAME TO replies;
CREATE UNIQUE INDEX idx_replies_uid ON replies(mailbox, imap_uid);
CREATE INDEX idx_replies_pending ON replies(processed_at);
CREATE INDEX idx_replies_workspace ON replies(workspace_id);

-- ---- notes ----
CREATE TABLE notes_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  body TEXT NOT NULL,
  author TEXT DEFAULT 'me',
  created_at TEXT
);
INSERT INTO notes_new (id, workspace_id, lead_id, body, author, created_at)
  SELECT id, 1, lead_id, body, author, created_at FROM notes;
DROP TABLE notes;
ALTER TABLE notes_new RENAME TO notes;
CREATE INDEX idx_notes_lead ON notes(lead_id);

-- ---- alerts ----
CREATE TABLE alerts_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  lead_id INTEGER REFERENCES leads(id),
  kind TEXT NOT NULL,
  severity TEXT DEFAULT 'info',
  title TEXT,
  body TEXT,
  channel TEXT,
  created_at TEXT
);
INSERT INTO alerts_new (id, workspace_id, lead_id, kind, severity, title,
  body, channel, created_at)
  SELECT id, 1, lead_id, kind, severity, title, body, channel, created_at
  FROM alerts;
DROP TABLE alerts;
ALTER TABLE alerts_new RENAME TO alerts;
CREATE INDEX idx_alerts_dedupe ON alerts(kind, lead_id, created_at);
CREATE INDEX idx_alerts_workspace ON alerts(workspace_id);

-- ---- offer_profiles (v2/01 seeded workspace_id nullable; now NOT NULL) ----
CREATE TABLE offer_profiles_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  product_description TEXT NOT NULL,
  value_prop TEXT NOT NULL,
  target_customer TEXT,
  tone TEXT DEFAULT 'direct, friendly',
  cta_style TEXT DEFAULT 'soft ask for a short reply',
  sender_name TEXT,
  sender_title TEXT,
  qualification_hints TEXT,
  disqualifiers TEXT,
  is_default INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0,
  created_at TEXT
);
INSERT INTO offer_profiles_new (id, workspace_id, name, product_description,
  value_prop, target_customer, tone, cta_style, sender_name, sender_title,
  qualification_hints, disqualifiers, is_default, deleted, created_at)
  SELECT id, COALESCE(workspace_id, 1), name, product_description,
    value_prop, target_customer, tone, cta_style, sender_name, sender_title,
    qualification_hints, disqualifiers, is_default, deleted, created_at
  FROM offer_profiles;
DROP TABLE offer_profiles;
ALTER TABLE offer_profiles_new RENAME TO offer_profiles;
CREATE INDEX idx_offers_workspace ON offer_profiles(workspace_id);
