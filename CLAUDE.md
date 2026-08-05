# press-release-intel — project roadmap

Press-release prospect intelligence pipeline. Ingests PR Newswire and GlobeNewswire wire feeds, extracts funding events, resolves company identity, and scores prospects.

This file is the durable plan of record for the roadmap below. Update it when a workstream's status changes — don't let it drift from what's actually built.

## Status legend
`todo` · `in progress` · `done` · `deferred`

---

## Product pivot (2026-08-05): funding-intelligence lead feed

**Why:** the switch detector's own backtest (cutoff 2024-06-01, run against the full corrected dataset) came back precision=0.07, recall=1.00, F1=0.13, on only 46 companies with a knowable outcome out of 5,619 company groups — real public data is too sparse in confirmed wire-switches to validate a switch detector as the core product, at least on the sources ingested so far. (A live investigation the same day found that Wayback CDX actually has dense, unindexed 2023–2025 Business Wire coverage — 13,000+ distinct releases/month, confirmed via full pagination — that could change this later. Not pursued now; flagged here so it isn't lost.)

**The pivot:** the product is now a **funding-intelligence lead feed** — structured extraction of funding announcements (who raised, how much, from whom, sector) from the VC feed and other wire sources, enriched with per-company activity profiles (cadence, momentum). The switch/gone-quiet signal from the old Track C detector is demoted to *one enrichment signal among several*, not the core product.

**Technical differentiator:** an ML-engineering layer that makes the extraction trustworthy enough to sell as a lead feed rather than a raw model dump — grounding (catching hallucinated fields), cost-weighted evaluation (a wrong field costs more than a missing one), uncertainty routing (low-confidence extractions go to human review, not straight to the feed), and null-model ablation (proving the LLM extractor earns its cost over a cheap heuristic baseline).

### 7-step roadmap

1. **Extraction grounding** (`in progress`) — post-extraction span-verification layer in `src/extract/extractor.py`: every field must be traceable to the source text or it's nulled and logged. Catches hallucination before it reaches eval or the feed.
2. **Cost-weighted evaluation** (`todo`, right after 1) — `src/eval/evaluator.py` gets a field-level error-cost model (company_name costliest, then amount_usd, investors, funding_round/sector) plus a false-claim penalty so a confidently wrong value scores worse than an honest null — rewarding Stage 1's grounding instead of fighting it.
3. **Uncertainty routing** (`todo`, later) — low-confidence extractions (ties into A1's proposed `extraction_confidence` field) route to human review instead of the feed directly.
4. **Null-model ablation** (`todo`, later) — benchmark the LLM extractor against a trivial baseline (regex/heuristic-only, or always-null) to prove it earns its cost rather than assuming it does.
5. **Per-company activity profiles** (`todo`, later) — generalize the cadence/momentum math already built in `src/detect/detector.py` beyond switch-detection into a general enrichment signal for the feed (posting frequency, recency, volume trend).
6. **Funding-feed product layer** (`not started` — next, pending review) — aggregate and rank extracted funding events into the actual lead feed; switch/gone-quiet becomes one input signal among several, not the headline.
7. **Delivery layer** (`not started` — next, pending review) — surfacing the feed (dashboard, alerts, or export). No UI work or new schema migrations without review first.

Steps 1–2 are the current work. Steps 6–7 and any new migrations are explicitly **not** started until reviewed.

---

## Track A — Extraction (AI centerpiece, elevated)

The extraction model (`src/extract/extractor.py`) turns press-release text into structured funding data. This track is the AI centerpiece of the project — extraction quality is the ceiling on everything downstream (funding_events, prospect scoring).

- **A1 — Expand the extraction schema** (`proposed` — design written up, awaiting review, not implemented): add `valuation_usd`, `total_raised_to_date_usd`, `is_extension_round`, `company_location`, and `extraction_confidence` (model self-reported, drives human-review routing for low-confidence extractions). Full field definitions, null/uncertainty handling, and gold-set implications: `docs/track-a1-schema-proposal.md`.
- **A2 — Model distillation and benchmarking** (`todo`, later): distill the Claude teacher extractor to a fine-tuned Qwen model. Benchmark 4–5 models — Claude, fine-tuned Qwen, untrained/base Qwen, and one other small model — on accuracy (against the hand-labeled gold set via `src/eval/evaluator.py`), cost, and speed. Include error analysis categorizing failure types (not just aggregate scores).

Existing: `src/extract/extractor.py` (Claude teacher extractor, `claude-opus-5`), `src/eval/evaluator.py` (scoring harness), `data/gold/releases.jsonl` (hand-labeled gold set).

---

## Track B — Historical switch-mining (detection data bootstrap)

The switch detector (Track C) needs historical (company, date, distributor) records to establish per-company baseline cadence and to mine known-switch examples for eventual model training. Track B is about *finding* that history cheaply, before building anything on top of it.

- **B1 — Historical metadata source investigation** (`done` — findings below; B2 mining work is still `todo`): tested real access to bulk historical (company, date, distributor) metadata (not full text) against wire sitemaps, GDELT, and the Internet Archive. Summary (full detail lives in the session that ran it — re-run the same checks if this needs refreshing):
  - **GlobeNewswire sitemap** (`sitemaps.globenewswire.com`) — **best source found.** Clean, monthly-partitioned XML sitemaps back to **2023-09** (~35 months), each entry carrying title, ISO publish timestamp, and stock ticker when applicable. No scraping needed, structured and reliable.
  - **PR Newswire live sitemap** — recent-only (Google News sitemap spec, ~48hr rolling window); `sitemap-company.xml` gives one URL per company with `lastmod` = most-recent-release date, useful as a cheap "still active on PRN" signal but not full per-release history.
  - **PR Newswire deep archive** (`sitemap-gz.xml` → monthly `.xml.gz` files, April 2011–September 2025, discovered via `robots.txt`) — **unreliable.** Every vintage tested (old and recent, small and large) downloads with `Content-Length` matching bytes received exactly, yet fails gzip integrity ("unexpected end of file") — the origin/CDN is serving broken files, not a network truncation on our end. Don't build against this without further diagnosis.
  - **GDELT live DOC 2.0 API** — persistently rate-limited from this environment's shared egress IP; not viable for bulk backfill anyway (GDELT's own error message redirects high-volume users to bulk files).
  - **GDELT bulk GKG files** (`data.gdeltproject.org/gdeltv2/*.gkg.csv.zip`, no rate limit, 15-minute cadence, **2015-02-18 to present**) — confirmed via direct download that a snapshot contains real `businesswire.com` URLs (direct release links and `cts.businesswire.com` tracking-redirect links carrying the BusinessWire story ID). Validates the hypothesis that GDELT can surface Business Wire history despite no live feed, but requires bulk-downloading and domain-filtering ~96 files/day across ~4,000 days — real engineering effort, not a quick query.
  - **Internet Archive / Wayback CDX API** — **second-strongest finding.** `businesswire.com/news/home/...` release permalinks are individually archived with the company/topic in the URL slug and the date embedded in the BusinessWire story ID (`YYYYMMDDNNNNNN`), confirmed present back to **at least 2003** — pre-dating GDELT's 2015 start and PRN's broken archive entirely. Pure CDX JSON (URL + timestamp + digest), zero full-text fetch required. Coverage *density* (are all releases captured, or a sample) wasn't fully characterized — worth a larger sampling pass before committing engineering time to a full backfill.
- **B2 — Retrospective switch mining** (`todo`, later): run the Track C detector logic retrospectively over the Track B historical data to mine past switches. Human spot-checks a sample before any of this feeds model training.

---

## Track C — Switch detector (demoted: now one enrichment signal, not the core product)

Built and working (C1–C3 done, C4 documented below), but its own backtest showed public data is too sparse in confirmed wire-switches to validate it as a standalone product signal (see the pivot section above). It's kept as-is and will feed into roadmap step 5/6 (per-company activity profiles, funding-feed enrichment) as one signal among several — not pursued further as a headline feature for now.

- **C1 — Distributor alias map** (`done`, `src/detect/wire_aliases.py`): normalizes wire brand aliases (e.g. our own "PR Newswire" / "PR Newswire - Venture Capital" feeds) before any switch logic runs.
- **C2 — Rules-based detector** (`done`, `src/detect/detector.py`): per-company baseline publishing cadence; "gone quiet" and "cadence drop" signals with recency decay and volume weighting.
- **C3 — Predictions log + outcome tracking** (`done` — table + backtest harness; not actively fed): `migrations/007_switch_predictions.sql`, `src/detect/backtest.py`. Backtested at cutoff 2024-06-01: precision=0.07, recall=1.00, F1=0.13 on 46 companies with a knowable outcome.
- **C4 — Documented upgrade trigger** (`deferred`): moot while C is demoted — revisit only if per-company activity profiles (roadmap step 5) surface switch-detection as valuable again, informed by the unindexed dense Business Wire data noted in the pivot section.

---

## Track D — Entity resolution (elevated)

Company identity resolution — the same company must map to one row across differently-formatted mentions across releases, or every downstream track (funding_events, switch cadence, prospect scores) silently double-counts or misses.

- **D1 — Name normalization + fuzzy matching** (`todo`): first pass, using `rapidfuzz` (already a dependency, already used in `src/eval/evaluator.py` for gold-set scoring) plus legal-suffix stripping (same normalization already built for `company_name` eval scoring — reusable). Builds on `public.companies.canonical_name` / `aliases` (`migrations/001_companies.sql`), which already has the shape for this.
- **D2 — Embedding-based resolution** (`todo`, later): for name changes and near-duplicates that fuzzy matching can't catch (rebrands, DBA names, subsidiary naming). Evaluated on real accumulated company data once there's enough of it to evaluate against.

---

## Execution order (as of this roadmap)

Superseded by the 2026-08-05 pivot above. Current order: **7-step roadmap steps 1 → 2 (this work) → (stop for review) → 3–7**. **Do not start step 6 (funding-feed product layer), step 7 (delivery/UI), or any new schema migrations until reviewed and explicitly greenlit.**

Track A/B/D below are still active and feed the pivot directly (A = the extraction this pivot depends on; D = entity resolution the feed needs; B is now optional/deferred since switch-mining isn't the priority). Track C is demoted per above but left intact.

## Existing implementation (context for the above)

- `src/ingest/` — RSS pollers for PR Newswire and GlobeNewswire (`migrations/002_releases.sql`), plus historical backfill (`historical.py`) and shared company-name heuristic (`company_name_heuristic.py`).
- `src/extract/extractor.py` — Claude teacher extraction (Track A); `company_backfill.py` — Haiku-based title-only company-name backfill.
- `src/eval/` — `label_gold.py` (hand-labeling CLI), `evaluator.py` (scoring harness).
- `src/resolve/resolver.py` — `company_group_key`/`normalize_company_name` (Track D first pass, done).
- `src/detect/` — switch detector (Track C, demoted — see above): `detector.py`, `wire_aliases.py`, `backtest.py`.
- `src/score/scorer.py` — placeholder for prospect scoring (downstream of Tracks A–D).
- `migrations/` — `companies`, `releases`, `extractions`, `funding_events`, `prospect_scores`, `switch_predictions`.
