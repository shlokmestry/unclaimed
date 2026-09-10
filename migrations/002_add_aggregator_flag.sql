-- Migration 002: probable-aggregator flag
--
-- Adds is_probable_aggregator to uncovered_agencies and backfills it against
-- existing rows with the same heuristic as
-- ingestion.enrichment.is_probable_aggregator() (kept in sync by hand — see
-- LOG.md "aggregator feeds" entry for why this exists): a "feed" is flagged
-- as a probable regional/national aggregator or multi-operator bundle,
-- rather than a single onboardable agency, if its name lists >= 2
-- comma-separated operators, or its route_count/stop_count is far outside
-- what any single real agency in this dataset has (a real single agency's
-- median is ~220 stops / ~15 routes; DELFI/BODS/ministries run to tens of
-- thousands).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, and the backfill UPDATE can be
-- re-run safely (it always recomputes from route_count/stop_count/name,
-- never accumulates).

ALTER TABLE uncovered_agencies
  ADD COLUMN IF NOT EXISTS is_probable_aggregator BOOLEAN;

UPDATE uncovered_agencies
SET is_probable_aggregator = (
  (LENGTH(name) - LENGTH(REPLACE(name, ',', ''))) >= 2
  OR COALESCE(route_count, 0) > 3000
  OR COALESCE(stop_count, 0) > 20000
);

CREATE INDEX IF NOT EXISTS ix_uncovered_agencies_is_probable_aggregator
  ON uncovered_agencies (is_probable_aggregator);
