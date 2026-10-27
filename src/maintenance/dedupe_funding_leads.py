"""One-off script: collapse funding_leads rows that describe the same
underlying funding round but were inserted from different release_ids (e.g.
the same round reported by multiple, separately-URLed wire releases --
release_id's own UNIQUE constraint can't catch this since each duplicate row
has a genuinely distinct release_id).

Duplicate group = same src.extract.leads_ingester.dedupe_key() computed
from the row's own fields -- the exact key the ingester writes -- so this
also collapses rows whose stored dedupe_key predates the v2 scheme (see
migrations/009_funding_leads_dedupe.sql). Within each group, the row with
the earliest created_at is kept; the rest are deleted.

Usage:
    python -m src.maintenance.dedupe_funding_leads             # dry run, reports only
    python -m src.maintenance.dedupe_funding_leads --execute   # actually deletes
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from src.db import get_client
from src.extract.leads_ingester import dedupe_key

_FETCH_PAGE_SIZE = 1000


def fetch_all_leads(client) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (
            client.table("funding_leads")
            .select("id,company_group_key,company_name,funding_round,amount_usd,published_at,created_at")
            .range(offset, offset + _FETCH_PAGE_SIZE - 1)
            .execute()
            .data
        )
        if not batch:
            break
        rows.extend(batch)
        offset += _FETCH_PAGE_SIZE
        if len(batch) < _FETCH_PAGE_SIZE:
            break
    return rows


def find_duplicate_groups(rows: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = dedupe_key(
            row["company_group_key"],
            row.get("funding_round"),
            row.get("amount_usd"),
            row["published_at"],
        )
        groups[key].append(row)
    return [group for group in groups.values() if len(group) > 1]


def run(execute: bool) -> None:
    client = get_client()
    rows = fetch_all_leads(client)
    dupe_groups = find_duplicate_groups(rows)
    total_extra = sum(len(group) - 1 for group in dupe_groups)

    print(f"Scanned {len(rows)} funding_leads rows table-wide.")
    print(
        f"Found {len(dupe_groups)} duplicate group(s), {total_extra} row(s) "
        f"to remove (keeping earliest created_at per group).\n"
    )

    to_delete: list[str] = []
    for group in dupe_groups:
        ordered = sorted(group, key=lambda r: r["created_at"])
        keep, drop = ordered[0], ordered[1:]
        print(f"  KEEP  {keep['id']}  {keep['company_name']!r}  created={keep['created_at']}")
        for row in drop:
            print(f"  DROP  {row['id']}  {row['company_name']!r}  created={row['created_at']}")
            to_delete.append(row["id"])
        print()

    if not execute:
        print("Dry run -- no rows deleted. Pass --execute to actually delete the DROP rows above.")
        return

    for lead_id in to_delete:
        client.table("funding_leads").delete().eq("id", lead_id).execute()
    print(f"Deleted {len(to_delete)} duplicate row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually delete duplicates (default: dry run).")
    args = parser.parse_args()
    run(args.execute)


if __name__ == "__main__":
    main()
