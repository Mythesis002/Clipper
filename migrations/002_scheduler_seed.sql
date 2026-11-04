-- Phase 1 forward-compatible additions. Safe to run after 001 on both fresh
-- and previously migrated scheduler databases.
CREATE TABLE IF NOT EXISTS provider_rate_limits (
    provider text PRIMARY KEY CHECK (provider IN ('meta','gemini','youtube')),
    capacity numeric NOT NULL CHECK (capacity >= 1),
    tokens numeric NOT NULL CHECK (tokens >= 0),
    refill_per_second numeric NOT NULL CHECK (refill_per_second > 0),
    last_refill_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS provider_circuits (
    provider text PRIMARY KEY REFERENCES provider_rate_limits(provider) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'closed' CHECK (state IN ('closed','open','half_open')),
    failure_count integer NOT NULL DEFAULT 0,
    opened_until timestamptz,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS scheduler_control (key text PRIMARY KEY, last_run_at timestamptz NOT NULL);
ALTER TABLE slots ADD COLUMN IF NOT EXISTS assignment_lead_minutes integer NOT NULL DEFAULT 360;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS verify_lead_minutes integer NOT NULL DEFAULT 30;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS container_lead_minutes integer NOT NULL DEFAULT 10;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS verification_checked_at timestamptz;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS quota_checked_at timestamptz;
ALTER TABLE slots ADD COLUMN IF NOT EXISTS quota_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;
INSERT INTO schema_migrations(version) VALUES ('002_quota_snapshot') ON CONFLICT DO NOTHING;
