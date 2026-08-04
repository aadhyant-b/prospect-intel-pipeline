# press-release-intel — project roadmap

Press-release prospect intelligence pipeline. Ingests PR Newswire and GlobeNewswire wire feeds, extracts funding events, detects distributor/PR-agency "switches" (a signal a company may be shopping for new representation), resolves company identity, and scores prospects.

This file is the durable plan of record for the roadmap below. Update it when a workstream's status changes — don't let it drift from what's actually built.

## Status legend
`todo` · `in progress` · `done` · `deferred`

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

## Track C — Switch detector (2 live wires: PR Newswire, GlobeNewswire)

Detects when a company appears to have changed PR distributor/agency — the core "prospect" signal (a company shopping for new representation is a sales lead).

- **C1 — Distributor alias map** (`todo`): a config recording that wire brands can be renamed, acquired, or co-owned (e.g. ACCESS owns Newswire — documented as the motivating example; no employer-specific data beyond public corporate-ownership facts). Normalization is applied *before* any switch logic runs, so a wire rebrand or ownership change never reads as a mass "switch" event across every company on that wire.
- **C2 — Rules-based detector** (`todo`): per-company baseline publishing cadence; "gone quiet" and "cadence drop" signals with recency decay and volume weighting (a company that posts weekly going silent for a month is a stronger signal than one that posts quarterly).
- **C3 — Predictions log + outcome tracking** (`todo`): a table logging every switch prediction plus its eventual outcome, so precision/recall become measurable over time and detection thresholds are tunable against real feedback rather than guessed once and left alone.
- **C4 — Documented upgrade trigger** (`todo`): once mined (Track B) + observed (C3) switches reach a sufficient labeled-example count, train a learned model and benchmark it against the tuned rules baseline before replacing it. The trigger condition (count threshold, benchmark bar) should be written down when C1–C3 land, not decided ad hoc later.

---

## Track D — Entity resolution (elevated)

Company identity resolution — the same company must map to one row across differently-formatted mentions across releases, or every downstream track (funding_events, switch cadence, prospect scores) silently double-counts or misses.

- **D1 — Name normalization + fuzzy matching** (`todo`): first pass, using `rapidfuzz` (already a dependency, already used in `src/eval/evaluator.py` for gold-set scoring) plus legal-suffix stripping (same normalization already built for `company_name` eval scoring — reusable). Builds on `public.companies.canonical_name` / `aliases` (`migrations/001_companies.sql`), which already has the shape for this.
- **D2 — Embedding-based resolution** (`todo`, later): for name changes and near-duplicates that fuzzy matching can't catch (rebrands, DBA names, subsidiary naming). Evaluated on real accumulated company data once there's enough of it to evaluate against.

---

## Execution order (as of this roadmap)

Explicitly sequenced by the user, not inferred: **B1 → A1 → (stop for review) → C → D**. B1 and A1 are done (findings/proposal above and in `docs/track-a1-schema-proposal.md`) and awaiting review. **Do not start Track C or D work, and do not implement A1's schema changes, until reviewed and explicitly greenlit.**

## Existing implementation (context for the above)

- `src/ingest/` — RSS pollers for PR Newswire and GlobeNewswire (`migrations/002_releases.sql`).
- `src/extract/extractor.py` — Claude teacher extraction (Track A).
- `src/eval/` — `label_gold.py` (hand-labeling CLI), `evaluator.py` (scoring harness).
- `src/resolve/resolver.py` — placeholder for Track D.
- `src/score/scorer.py` — placeholder for prospect scoring (downstream of Tracks A–D).
- `migrations/` — `companies`, `releases`, `extractions`, `funding_events`, `prospect_scores`. No switch-detection or switch-prediction tables yet (Track C, C3).
