-- 0005 lead query indexes: the table view (pipeline redesign) sorts by
-- updated_at and filters by score inside one workspace; 0002 only indexed
-- (workspace_id, status) and (workspace_id, email).

CREATE INDEX idx_leads_ws_updated ON leads(workspace_id, updated_at);
CREATE INDEX idx_leads_ws_score ON leads(workspace_id, score);
