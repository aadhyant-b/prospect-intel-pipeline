-- 008_funding_leads.sql
-- PROPOSED -- not yet applied. Documents the live Supabase schema once run;
-- applied manually, not via a migration runner (same convention as 001-007).

CREATE TABLE public.funding_leads (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  release_id          uuid NOT NULL UNIQUE REFERENCES public.releases(id),
  extraction_id       uuid REFERENCES public.extractions(id),

  -- Interim identity (src/resolve/resolver.py::company_group_key), same
  -- pattern as switch_predictions -- company_id stays nullable until
  -- Track D resolves real entities.
  company_group_key   text NOT NULL,
  company_id          uuid REFERENCES public.companies(id),

  -- Grounded extraction fields (already verified by
  -- src/extract/extractor.py::ground_extraction -- ungrounded values are
  -- null/dropped before they ever reach this table).
  company_name        text,
  funding_round        text,
  amount_usd           numeric,
  investors             jsonb NOT NULL DEFAULT '[]'::jsonb,
  sector                text,

  -- Denormalized from releases so freshness/filter queries ("this week's
  -- leads", "leads from GlobeNewswire") don't need a join.
  published_at         timestamptz NOT NULL,
  distributor           text NOT NULL,

  -- Grounding/confidence metadata for the UI: which fields failed
  -- verification and why (GroundedExtractionResult.grounding_failures,
  -- same shape). fully_grounded is a cheap denormalized flag (true iff
  -- grounding_failures is empty) so the UI can filter/sort on confidence
  -- without unpacking the jsonb array on every query.
  grounding_failures    jsonb NOT NULL DEFAULT '[]'::jsonb,
  fully_grounded        boolean NOT NULL DEFAULT true,

  model_version         text NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX funding_leads_published_at_idx       ON public.funding_leads(published_at DESC);
CREATE INDEX funding_leads_company_group_key_idx  ON public.funding_leads(company_group_key);
CREATE INDEX funding_leads_company_id_idx         ON public.funding_leads(company_id);
CREATE INDEX funding_leads_fully_grounded_idx     ON public.funding_leads(fully_grounded);

COMMENT ON COLUMN public.funding_leads.company_group_key IS
  'Normalized grouping key from src/resolve/resolver.py -- the operative identity until Track D resolves company_id. Also the join key into switch_predictions for read-time enrichment (see plan notes) -- no FK, deliberately loosely coupled.';
COMMENT ON COLUMN public.funding_leads.grounding_failures IS
  'List of human-readable strings from GroundedExtractionResult.grounding_failures, e.g. ["investors: '\''Fake Ventures'\'' not found in source text"]. Empty list = every extracted field was verified.';
COMMENT ON COLUMN public.funding_leads.fully_grounded IS
  'Denormalized: true iff grounding_failures is empty. Lets the UI filter/sort "fully verified" leads without parsing jsonb.';
