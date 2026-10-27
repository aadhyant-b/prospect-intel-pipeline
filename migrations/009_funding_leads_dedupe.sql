-- 009_funding_leads_dedupe.sql
-- Documents the live Supabase schema: this column and index already exist
-- (added by hand, not via a migration runner -- same convention as 001-008).
-- An earlier draft of this file defined dedupe_key as a GENERATED plaintext
-- column; that was never what got applied. Live, it is a plain NOT NULL text
-- column the application populates.
--
-- Fresh environments: run python -m src.maintenance.dedupe_funding_leads
-- --execute before creating the unique index if funding_leads already has
-- rows, or the index will fail to build over existing duplicates.

ALTER TABLE public.funding_leads
  ADD COLUMN dedupe_key text NOT NULL;

CREATE UNIQUE INDEX funding_leads_dedupe_key_idx ON public.funding_leads(dedupe_key);

COMMENT ON COLUMN public.funding_leads.dedupe_key IS
  'Stable identity for "the same funding round", independent of which wire
  release (or language version of it) described it. Computed in Python by
  src/extract/leads_ingester.py::dedupe_key(): md5 of
  "v2|company_group_key|funding_round-or-unknown|amount_usd-to-3-sig-figs-or-none",
  with "|YYYY-MM" (UTC publish month) appended only when funding_round is
  other/null AND amount_usd is null. The ingester upserts on this column
  (on_conflict="dedupe_key"). Rows written before v2 carry 32-hex keys whose
  original inputs were never recovered; they must be recomputed with
  dedupe_key() before they dedupe against new rows.';
