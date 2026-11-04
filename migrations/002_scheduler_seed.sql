-- Phase 1 forward-compatible additions. Safe to run after 001 on both fresh
-- and previously migrated scheduler databases.
CREATE TABLE IF NOT EXISTS scheduler_control (key text PRIMARY KEY, last_run_at timestamptz NOT NULL);
ALTER TABLE slots ADD COLUMN IF NOT EXISTS verification_checked_at timestamptz;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS quota_checked_at timestamptz;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS quota_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;
INSERT INTO schema_migrations(version) VALUES ('002_quota_snapshot') ON CONFLICT DO NOTHING;
