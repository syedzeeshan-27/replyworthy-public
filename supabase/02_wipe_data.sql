-- Clean slate: deletes EVERY workspace, account row, lead, campaign, email,
-- reply, note, alert, offer, mailbox and billing record, then puts back the
-- empty owner workspace (id 1). The tables themselves stay.
--
-- Paste into the Supabase SQL editor and press Run. There is no undo.
-- Restart the app afterwards so it re-creates the default offer.
--
-- Not touched on purpose:
--   * hard_suppression  people who unsubscribed stay unsubscribed.
--   * Supabase logins   (Authentication -> Users in the dashboard). Anyone
--                       who logs in again simply gets a fresh empty workspace.
-- To clear those too, run the two lines at the bottom (remove the leading --).

BEGIN;

TRUNCATE TABLE
  replies, sends, sequence_steps, notes, alerts, leads, campaigns,
  suppression, offer_profiles, mailboxes, credit_ledger, billing_events,
  users, workspaces
RESTART IDENTITY CASCADE;

INSERT INTO workspaces (id, name, status, plan, live_sending, warmup_ack, created_at)
VALUES (1, 'My workspace', 'active', 'internal', 1, 1,
        to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'));
SELECT setval(pg_get_serial_sequence('workspaces', 'id'), 1);

COMMIT;

-- TRUNCATE TABLE hard_suppression;
-- DELETE FROM auth.users;
