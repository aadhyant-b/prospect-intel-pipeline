"""One-time backfill: populate company_name_raw for live releases that
predate the src/ingest/pollers.py change computing it at poll time.

252 live rows were ingested before that change and have company_name_raw =
NULL -- the switch detector (src/detect/detector.py) can't group them until
this runs. Two-stage pipeline per row, cheapest first:

1. The same free regex heuristic historical.py/pollers.py use
   (src/ingest/company_name_heuristic.py::guess_company_name).
2. For rows the regex can't resolve, the same cheap Claude Haiku pass
   src/extract/company_backfill.py already built for historical rows --
   reused here, not reimplemented, including its junk-title filtering and
   its '' ("attempted, confirmed no company") vs NULL ("never attempted")
   idempotency convention, so reruns only touch rows still genuinely
   unprocessed.

Run via `python -m src.ingest.backfill_live_company_names [--limit N] [--no-haiku]`.
"""

from __future__ import annotations

import argparse

import anthropic

from src.db import get_client
from src.extract.company_backfill import _should_skip, extract_company_name
from src.ingest.company_name_heuristic import guess_company_name


def _fetch_null_live_rows(limit: int | None) -> list[dict]:
    query = get_client().table("releases").select("id,title").eq("source", "live").is_("company_name_raw", "null")
    if limit is not None:
        query = query.limit(limit)
    return query.execute().data


def run(limit: int | None, use_haiku: bool) -> None:
    db = get_client()
    rows = _fetch_null_live_rows(limit)

    filled_by_regex = 0
    filled_by_haiku = 0
    confirmed_no_company = 0
    junk_skipped = 0
    total_cost = 0.0

    anthropic_client = anthropic.Anthropic() if use_haiku else None

    for row in rows:
        title = (row["title"] or "").strip()

        if not title or _should_skip(title):
            junk_skipped += 1
            db.table("releases").update({"company_name_raw": ""}).eq("id", row["id"]).execute()
            print(f"[skip-junk] {title!r}")
            continue

        regex_name = guess_company_name(title)
        if regex_name:
            filled_by_regex += 1
            db.table("releases").update({"company_name_raw": regex_name}).eq("id", row["id"]).execute()
            print(f"[regex]     {title[:70]!r} -> {regex_name!r}")
            continue

        if not use_haiku:
            print(f"[no-match]  {title[:70]!r} -> None (regex only, left NULL)")
            continue

        haiku_name, cost = extract_company_name(anthropic_client, title)
        total_cost += cost
        db.table("releases").update({"company_name_raw": haiku_name or ""}).eq("id", row["id"]).execute()
        if haiku_name:
            filled_by_haiku += 1
        else:
            confirmed_no_company += 1
        print(f"[haiku $ {cost:.6f}] {title[:60]!r} -> {haiku_name!r}")

    print(
        f"\nProcessed {len(rows)} rows: filled_by_regex={filled_by_regex} "
        f"filled_by_haiku={filled_by_haiku} confirmed_no_company={confirmed_no_company} "
        f"junk_skipped={junk_skipped}. Estimated Haiku cost: ${total_cost:.4f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time backfill of company_name_raw for pre-existing live releases."
    )
    parser.add_argument("--limit", type=int, default=None, help="Max rows to process (default: all NULL live rows).")
    parser.add_argument(
        "--no-haiku", action="store_true",
        help="Regex pass only -- leave rows the heuristic can't resolve as NULL instead of calling Claude Haiku.",
    )
    args = parser.parse_args()
    run(args.limit, use_haiku=not args.no_haiku)


if __name__ == "__main__":
    main()
