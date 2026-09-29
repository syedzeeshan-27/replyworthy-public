-- 0005 lead query indexes — Postgres twin of the SQLite file.

CREATE INDEX idx_leads_ws_updated ON leads(workspace_id, updated_at);
CREATE INDEX idx_leads_ws_score ON leads(workspace_id, score);
