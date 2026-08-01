# Gold set

`releases.jsonl` is a hand-labeled, quarantined test set for evaluating extraction models. It is never written to by the ingest/extract pipeline — only `src/eval/label_gold.py` appends to it, and only from a human's input.

One JSON object per line:

```json
{
  "release_id": "uuid, from releases.id",
  "url": "from releases.url",
  "distributor": "from releases.distributor",
  "published_at": "from releases.published_at",
  "is_funding_related": true,
  "company_name": "string or null",
  "funding_round": "pre-seed | seed | series-a | series-b | series-c | series-d-plus | debt | grant | ipo | other | null",
  "amount_usd": 12000000,
  "investors": ["string", "..."],
  "sector": "string or null",
  "labeled_at": "ISO 8601 timestamp",
  "labeler_notes": "string or null"
}
```

Non-funding releases are recorded with `is_funding_related: false` and null/empty fields — these are negative controls, used to check whether an extraction model wrongly pulls funding fields out of unrelated text.
