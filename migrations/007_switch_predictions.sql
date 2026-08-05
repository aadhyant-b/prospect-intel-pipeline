-- 007_switch_predictions.sql
-- Documents the live Supabase schema; applied manually, not via a migration runner.

CREATE TABLE public.switch_predictions (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_group_key       text NOT NULL,
  company_id              uuid REFERENCES public.companies(id),
  score                   numeric NOT NULL,
  reasons                 jsonb NOT NULL,
  signals                 jsonb NOT NULL,
  most_recent_release_id  uuid REFERENCES public.releases(id),
  detected_at             timestamptz NOT NULL DEFAULT now(),
  outcome                 text,
  outcome_notes           text,
  outcome_recorded_at     timestamptz
);

CREATE INDEX switch_predictions_company_group_key_idx ON public.switch_predictions(company_group_key);
CREATE INDEX switch_predictions_detected_at_idx ON public.switch_predictions(detected_at);

COMMENT ON COLUMN public.switch_predictions.company_group_key IS
  'The normalized grouping key from src/resolve/resolver.py -- the operative identity until Track D resolves company_id.';
COMMENT ON COLUMN public.switch_predictions.reasons IS
  'List of human-readable strings, one per active signal (e.g. ["Gone quiet: ...", "Wire change: ..."]).';
COMMENT ON COLUMN public.switch_predictions.signals IS
  'Raw per-signal numbers (gone_quiet_score, cadence_drop_score, wire_change_score, volume_weight, baseline_gap_days, n_releases) for later threshold-tuning.';
COMMENT ON COLUMN public.switch_predictions.outcome IS
  'null until reviewed; ''confirmed_switch'' | ''false_positive'' | ''unknown''. Populated automatically by src/detect/backtest.py, or manually for live predictions.';
