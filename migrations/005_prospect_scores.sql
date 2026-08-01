-- Documents the live Supabase schema; applied manually, not via a migration runner.

CREATE TABLE public.prospect_scores (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid REFERENCES public.companies(id),
  week       date,
  score      numeric,
  reasons    jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prospect_scores_company_week_idx ON public.prospect_scores(company_id, week);
