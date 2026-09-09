-- Migration 001: opportunity-score rework + readiness status
--
-- Adds the columns needed for the new weighted opportunity_score formula
-- and the readiness_status classification. Safe to run against a database
-- that already has the original schema (Step 1-10) — every ADD COLUMN is
-- guarded with IF NOT EXISTS, so this is idempotent (safe to re-run).
--
-- Run this BEFORE re-running the enrichment pipeline (which populates the
-- new columns) — see the "Manual steps" list for exact order.

ALTER TABLE mobility_agencies
  ADD COLUMN IF NOT EXISTS feed_last_updated TIMESTAMP;

ALTER TABLE uncovered_agencies
  ADD COLUMN IF NOT EXISTS has_realtime BOOLEAN,
  ADD COLUMN IF NOT EXISTS feed_last_updated TIMESTAMP,
  ADD COLUMN IF NOT EXISTS route_count INTEGER,
  ADD COLUMN IF NOT EXISTS stop_count INTEGER,
  ADD COLUMN IF NOT EXISTS trip_count INTEGER,
  ADD COLUMN IF NOT EXISTS readiness_status VARCHAR;

CREATE INDEX IF NOT EXISTS ix_uncovered_agencies_readiness_status
  ON uncovered_agencies (readiness_status);
