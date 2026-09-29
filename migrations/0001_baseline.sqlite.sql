-- 0001 baseline: the complete pre-Stage-4 (single-tenant) schema.
-- Fresh DBs run this file; DBs created before the migration runner existed
-- are detected in tools/db.py, patched with the late-added columns, and have
-- this migration recorded without running the file.

CREATE TABLE leads (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  place_id       TEXT UNIQUE,
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
CREATE INDEX idx_leads_status ON leads(status);
CREATE INDEX idx_leads_email ON leads(email);

CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT,
  city TEXT,
  status TEXT DEFAULT 'draft',
  daily_cap INTEGER DEFAULT 30,
  created_at TEXT,
  offer_id INTEGER
);

CREATE TABLE sequence_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  step_number INTEGER NOT NULL,
  delay_days INTEGER NOT NULL,
  subject_template TEXT,
  body_template TEXT
);

CREATE TABLE sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
CREATE INDEX idx_sends_due ON sends(status, scheduled_at);
CREATE INDEX idx_sends_lead ON sends(lead_id);

CREATE TABLE suppression (
  email TEXT PRIMARY KEY,
  reason TEXT,
  added_at TEXT
);

CREATE TABLE replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
CREATE UNIQUE INDEX idx_replies_uid ON replies(mailbox, imap_uid);
CREATE INDEX idx_replies_pending ON replies(processed_at);

CREATE TABLE notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  body TEXT NOT NULL,
  author TEXT DEFAULT 'me',
  created_at TEXT
);
CREATE INDEX idx_notes_lead ON notes(lead_id);

CREATE TABLE alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER REFERENCES leads(id),
  kind TEXT NOT NULL,
  severity TEXT DEFAULT 'info',
  title TEXT,
  body TEXT,
  channel TEXT,
  created_at TEXT
);
CREATE INDEX idx_alerts_dedupe ON alerts(kind, lead_id, created_at);

CREATE TABLE offer_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id INTEGER,
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
