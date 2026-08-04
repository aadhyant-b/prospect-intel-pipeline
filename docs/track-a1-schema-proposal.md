# A1 — Expanded extraction schema (proposal, not yet implemented)

Status: **plan only**. Nothing in `src/extract/extractor.py`, `src/eval/evaluator.py`, `data/gold/`, or `src/eval/label_gold.py` has been changed for this. Written up here so it can be reviewed before any of those files are touched.

## Current schema (baseline)

`ExtractionResult` in `src/extract/extractor.py`, mirrored by `data/gold/releases.jsonl`:

```python
is_funding_related: bool
company_name: str | None
funding_round: FundingRound | None   # closed vocab
amount_usd: float | None
investors: list[str]
sector: str | None
```

Gating rule already in place: the non-`is_funding_related` fields are only meaningful (and only ever populated) on rows where `is_funding_related` is `True`.

## Proposed new fields

```python
valuation_usd: float | None
total_raised_to_date_usd: float | None
is_extension_round: bool | None
company_location: str | None
extraction_confidence: Literal["low", "medium", "high"]
```

### `valuation_usd`

The company's valuation associated with this round, in USD, as a number — e.g. "...at a $6.8 billion valuation" → `6800000000.0`.

- **Null handling**: `None` when no valuation is stated (most releases don't disclose one) — same "don't invent it" rule as `amount_usd`.
- **Uncertainty**: press releases sometimes state pre-money and sometimes post-money valuation without saying which. Rather than adding a separate `valuation_type` field (which the roadmap didn't ask for and would be scope creep), the extraction prompt will instruct the model to record whatever single number is stated and prefer the post-money figure if both are given — this is a pragmatic default, not a guarantee of which basis the number is on. If precise pre/post distinction turns out to matter downstream, that's a natural follow-up field, not part of this pass.
- **Gating**: null on non-funding rows, same as the existing fields.

### `total_raised_to_date_usd`

Cumulative funding raised across all rounds, as explicitly stated in the release — e.g. "...bringing total funding to $120 million" → `120000000.0`. This is distinct from `amount_usd` (this round only).

- **Null handling**: `None` unless the release explicitly states a cumulative figure. The model must not compute this by summing `amount_usd` across releases or recalling outside knowledge — only extract what's stated in this specific text. Worth stating explicitly in the prompt, since it's the one field where a model might be tempted to "helpfully" infer a number it wasn't given.
- **Gating**: null on non-funding rows.

### `is_extension_round`

Whether the release describes this round as an extension/top-up of a previously announced round (e.g. "Series B extension," "extends its $40M Series A").

- **Type reasoning**: unlike `amount_usd`/`valuation_usd`, absence of extension language is itself informative (most rounds aren't extensions), so this isn't a "the model doesn't know" case the way an undisclosed amount is. On funding-related rows the model should always be able to answer `True` or `False` — it's `None` only because the field is gated (non-funding rows), not because "extension-ness" is genuinely unknowable when it does apply.
- **Gating**: null on non-funding rows; `True`/`False` (never null) on funding-related rows.

### `company_location`

Free text company location as stated — e.g. `"San Francisco, CA"`, `"London, UK"`. Not normalized/geocoded at this stage (that would be a Track D concern, not extraction).

- **Null handling**: `None` when no location is stated or determinable from the release text.
- **Gating**: null on non-funding rows, consistent with `company_name`/`sector` — the current schema only captures company detail fields when the release is actually a funding announcement, and this follows that pattern rather than becoming the first field that's independent of `is_funding_related`.

### `extraction_confidence`

The model's self-reported confidence in the **overall extraction** (not per-field), for routing low-confidence extractions to human review before they're trusted (e.g. as training data for the Track A2 distillation, or fed into `funding_events`).

- **Type**: an ordinal `Literal["low", "medium", "high"]` rather than a continuous `0.0–1.0` float. LLM-reported confidence scores are well known to be poorly calibrated at fine granularity (a model saying "0.87" vs "0.91" isn't meaningfully different), and the actual use case — a review-routing threshold — only needs a small number of buckets. Three levels is deliberately coarse: "low → always review," "medium → spot-check," "high → trust by default" is a decision a human can operationalize immediately, versus tuning a float threshold that implies false precision.
- **Gating**: unlike the other new fields, this is **not** gated by `is_funding_related` — it should be populated on every extraction, including non-funding ones, because the `is_funding_related` classification itself is the highest-stakes call the model makes (it gates everything else), and a low-confidence "not funding-related" call is exactly the kind of row that should get a second look.
- **Prompt guidance**: instruct the model to report `"low"` when the release is ambiguous about funding-relatedness, uses unusual terminology, or key fields (round type, amount) are implied rather than stated outright; `"high"` when the release is an unambiguous, clearly-worded funding announcement.

## Gold-set implications

This is the part that needs a decision before implementation, since it affects three files (`data/gold/README.md`, `src/eval/label_gold.py`, `src/eval/evaluator.py`), not just the extractor.

1. **Existing 25 gold rows have no values for the 5 new fields.** Two options:
   - **(a) Backfill.** Re-run a modified `label_gold.py` in an "augment" mode over the existing labeled `release_id`s, prompting the human labeler only for the 5 new fields on rows already marked `is_funding_related: true` (no need to re-review non-funding rows for `company_location`/`valuation_usd`/etc. since those stay null there anyway; `extraction_confidence` has no human-labelable ground truth at all — see point 3).
   - **(b) Leave legacy rows with the new fields absent/null**, and have the eval harness treat "field absent in gold record" as "don't score this field for this row" rather than "score as null." This is less work but means the new fields are under-evaluated until enough freshly-labeled examples with the extension exist.
   - Recommendation: (a) for `valuation_usd`, `total_raised_to_date_usd`, `is_extension_round`, and `company_location` — it's a small, fast pass (25 rows, mostly re-reading press releases already open during original labeling) and gives real eval coverage immediately, rather than waiting on new gold growth.
2. **`extraction_confidence` is never gold-labelable.** There's no "true" confidence for a human to write down — confidence is inherently a property of the model's own uncertainty, not a fact about the release. So it's excluded from `data/gold/releases.jsonl` and from `src/eval/evaluator.py` scoring entirely; it's consumed downstream by whatever human-review routing logic gets built later, not by the eval harness.
3. **Scoring approach for the 4 gold-labelable new fields** (for when `src/eval/evaluator.py` is actually extended — not done in this pass):
   - `valuation_usd`, `total_raised_to_date_usd`: exact numeric match with null handling, same pattern as the existing `amount_usd` field.
   - `is_extension_round`: exact boolean match (small confusion-matrix-style breakdown optional, same idea as `is_funding_related` but lower stakes since it doesn't gate other fields).
   - `company_location`: fuzzy match, same mechanism/threshold as `company_name`/`sector` (free text, not a closed vocabulary).
4. **Extractor prompt cost**: five more fields in the schema and slightly more system-prompt guidance. Still a small, bounded JSON output — no meaningful change to the per-release cost profile already established (`src/extract/extractor.py`'s cost-tracking CLI).

## What's explicitly out of scope for this pass

- No `valuation_type` (pre/post-money) field — noted above as a possible future addition if it turns out to matter, not built preemptively.
- No geocoding/normalization of `company_location` — raw text only; structured location resolution would be a Track D-adjacent concern.
- No change to `funding_round`'s closed vocabulary — extension rounds are captured via the new boolean, not by adding round-type variants like `"series-b-extension"`.
