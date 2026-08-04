-- 006_release_provenance.sql
-- Documents the live Supabase schema; applied manually, not via a migration runner.

ALTER TABLE public.releases
  ADD COLUMN source text NOT NULL DEFAULT 'live',
  ADD COLUMN company_name_raw text;

COMMENT ON COLUMN public.releases.source IS
  'Ingestion provenance: ''live'' (RSS pollers), ''globenewswire-sitemap'', or ''wayback-businesswire''. Coverage completeness differs by source -- the switch detector must not treat a sparse period on a backfilled source as evidence of reduced publishing cadence without accounting for that.';

COMMENT ON COLUMN public.releases.company_name_raw IS
  'Best-effort company name parsed at ingestion time from title/URL slug, for sources that bypass the extraction pipeline (no raw_text to extract from). NULL for live rows, which get company identity from extractions.fields instead. Not resolved to companies.id -- that is Track D''s job.';

CREATE INDEX releases_source_idx ON public.releases(source);
