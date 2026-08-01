-- Documents the live Supabase schema; applied manually, not via a migration runner.

CREATE TABLE public.funding_events (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id        uuid REFERENCES public.companies(id),
  round             text,
  amount_usd        numeric,
  announced_at      timestamptz,
  source_release_id uuid REFERENCES public.releases(id)
);

CREATE INDEX funding_events_company_id_idx ON public.funding_events(company_id);
