-- Documents the live Supabase schema; applied manually, not via a migration runner.

CREATE TABLE public.extractions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  release_id    uuid REFERENCES public.releases(id),
  model_version text,
  fields        jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX extractions_release_id_idx ON public.extractions(release_id);
