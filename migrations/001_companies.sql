-- Documents the live Supabase schema; applied manually, not via a migration runner.

CREATE TABLE public.companies (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  canonical_name text NOT NULL,
  aliases        text[],
  sector         text,
  first_seen     timestamptz,
  last_seen      timestamptz
);
