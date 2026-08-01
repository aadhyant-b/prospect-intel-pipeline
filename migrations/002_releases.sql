-- Documents the live Supabase schema; applied manually, not via a migration runner.

CREATE TABLE public.releases (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid REFERENCES public.companies(id),
  url           text UNIQUE,
  distributor   text,
  published_at  timestamptz,
  title         text,
  raw_text      text,
  fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX releases_company_id_idx   ON public.releases(company_id);
CREATE INDEX releases_published_at_idx ON public.releases(published_at);
CREATE INDEX releases_distributor_idx  ON public.releases(distributor);
