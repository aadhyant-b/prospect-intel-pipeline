"""Funding-leads ingester (roadmap step 6): the product layer that finally
writes real rows into `extractions` and `funding_leads`.

Pulls funding-related-candidate releases (default source: the PR Newswire
Venture Capital feed -- the only feed this ever runs against, since it's
already funding-dense; general wire feeds are noise-dominated and not
worth AI spend here) that haven't been processed yet, RANKS them with a
free (no-API) title-keyword + recency score (see _title_score /
_fetch_candidate_releases), and only sends the top `limit` to the grounded
extractor (src/extract/extractor.py, Stage 1) -- ranking happens before
any AI spend, so cost stays proportional to `limit`, not to backlog size.
Every attempt is logged to `extractions` (funding-related or not -- this
is what makes reruns idempotent, see _fetch_processed_release_ids), and a
`funding_leads` row is written for each one that is funding-related and
has a grounded company name.

Read-only by default -- pass --write to actually insert. Matches the
convention already established by src/detect/detector.py and
src/extract/company_backfill.py.

Run via `python -m src.extract.leads_ingester [--source ...] [--limit N] [--write]`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime

import anthropic
from dotenv import load_dotenv

from src.db import get_client
from src.extract.extractor import DEFAULT_MODEL, extract_release
from src.resolve.resolver import company_group_key

load_dotenv()

DEFAULT_SOURCE = "PR Newswire - Venture Capital"
DEFAULT_LIMIT = 20
_FETCH_PAGE_SIZE = 1000

# Free (no-API) title signals used to rank candidates before any AI spend.
# Phrase hits are the base score; an explicit dollar amount is a strong
# extra signal since it correlates with an actual disclosed raise rather
# than a vague funding-adjacent mention.
_TITLE_KEYWORDS = [
    "raises", "raised", "raising",
    "secures", "closes",
    "series a", "series b", "series c",
    "seed round", "seed funding", "seed extension", "pre-seed",
    "funding round", "closes funding", "closes financing",
    "secures funding", "secures investment", "secures financing",
    "investment from", "venture round", "growth equity", "term loan",
]
_AMOUNT_RE = re.compile(r"\$\s?\d[\d,.]*\s?(million|billion|m|mm|bn|b)\b", re.IGNORECASE)


# Bumped whenever the dedupe_key inputs change, so keys from different
# schemes can never collide silently. (Pre-v2 rows carry 32-hex keys whose
# original inputs couldn't be recovered from git or reproduced -- they need
# a one-off recompute with dedupe_key() before they dedupe against new rows.)
_DEDUPE_KEY_VERSION = "v2"
# A round this weakly identified (no specific round, no amount) is only
# distinguishable from a later raise by the same company by when it was
# announced, so its key is additionally bucketed by UTC month.
_WEAK_ROUNDS = {None, "other"}


def _amount_bucket(amount_usd: float | None) -> str:
    """amount_usd to 3 significant figures ("1.52e+08"), or "none". 3 sig figs
    absorbs currency-conversion drift between language versions of one
    announcement (EUR 140M ~= $151.8M -> 1.52e+08, same as $152M) without
    merging genuinely different raises like $152M and $148M."""
    if amount_usd is None:
        return "none"
    return f"{float(amount_usd):.2e}"


def dedupe_key(
    company_group_key: str,
    funding_round: str | None,
    amount_usd: float | None,
    published_at: str,
) -> str:
    """Stable identity for one funding round, independent of which wire
    release (or which language version of it) reported it: md5 of version |
    company_group_key | funding_round | 3-sig-fig amount bucket, plus the UTC
    publish month only when the round is weak (round other/null AND no
    amount). published_at is otherwise deliberately excluded -- translated
    re-releases routinely land hours later, often across a UTC day boundary."""
    parts = [
        _DEDUPE_KEY_VERSION,
        company_group_key,
        funding_round or "unknown",
        _amount_bucket(amount_usd),
    ]
    if funding_round in _WEAK_ROUNDS and amount_usd is None:
        published = datetime.fromisoformat(published_at)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        parts.append(published.astimezone(UTC).strftime("%Y-%m"))
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _title_score(title: str) -> int:
    """Free relevance score from the title alone -- no API call. Funding
    phrase hits, plus +2 for an explicit dollar amount."""
    t = (title or "").lower()
    score = sum(1 for kw in _TITLE_KEYWORDS if kw in t)
    if _AMOUNT_RE.search(t):
        score += 2
    return score


def _fetch_processed_release_ids(client) -> set[str]:
    """release_ids already logged to `extractions` -- funding-related or
    not, an attempted extraction is an attempted extraction. Paginated since
    PostgREST caps a single .execute() at 1000 rows."""
    ids: set[str] = set()
    offset = 0
    while True:
        batch = (
            client.table("extractions")
            .select("release_id")
            .range(offset, offset + _FETCH_PAGE_SIZE - 1)
            .execute()
            .data
        )
        if not batch:
            break
        ids.update(row["release_id"] for row in batch)
        offset += _FETCH_PAGE_SIZE
        if len(batch) < _FETCH_PAGE_SIZE:
            break
    return ids


def _fetch_candidate_releases(client, source: str, limit: int) -> list[dict]:
    """Every not-yet-processed release for `source`, ranked by the free
    title/recency score and sliced to the top `limit`. Two passes so AI-free
    ranking runs over cheap metadata (id/title/published_at) even when the
    backlog is large, and raw_text (the only large field) is fetched only
    for the exact rows that will actually be sent to the extractor."""
    processed_ids = _fetch_processed_release_ids(client)

    meta: list[dict] = []
    offset = 0
    while True:
        batch = (
            client.table("releases")
            .select("id,title,published_at")
            .eq("distributor", source)
            .range(offset, offset + _FETCH_PAGE_SIZE - 1)
            .execute()
            .data
        )
        if not batch:
            break
        meta.extend(row for row in batch if row["id"] not in processed_ids)
        offset += _FETCH_PAGE_SIZE
        if len(batch) < _FETCH_PAGE_SIZE:
            break

    # Highest title score first; ties broken by recency (ISO published_at
    # sorts correctly as a string). Free signals only -- no AI spend yet.
    meta.sort(key=lambda r: (_title_score(r["title"]), r["published_at"] or ""), reverse=True)
    top_ids = [row["id"] for row in meta[:limit]]
    if not top_ids:
        return []

    full = (
        client.table("releases")
        .select("id,title,raw_text,distributor,published_at")
        .in_("id", top_ids)
        .execute()
        .data
    )
    by_id = {row["id"]: row for row in full}
    return [by_id[i] for i in top_ids if i in by_id]


def run(source: str, limit: int, write: bool) -> None:
    client = get_client()
    anthropic_client = anthropic.Anthropic()
    rows = _fetch_candidate_releases(client, source, limit)

    print(f"Found {len(rows)} unprocessed release(s) for source={source!r}, "
          f"ranked by free title/recency score (limit={limit})\n")

    total_cost = 0.0
    n_leads = 0
    n_skipped_no_company = 0
    n_not_funding = 0

    for row in rows:
        grounded, cost = extract_release(anthropic_client, row["title"], row["raw_text"] or "")
        total_cost += cost

        extraction_id = None
        if write:
            result = (
                client.table("extractions")
                .insert({
                    "release_id": row["id"],
                    "model_version": DEFAULT_MODEL,
                    "fields": json.dumps(grounded.model_dump()),
                })
                .execute()
            )
            extraction_id = result.data[0]["id"] if result.data else None

        if not grounded.is_funding_related:
            n_not_funding += 1
            print(f"[${cost:.5f}] {row['title'][:70]!r} -> not funding-related")
            continue

        group_key = company_group_key(grounded.company_name)
        if group_key is None:
            # Funding-related but company_name failed grounding (or was
            # never extracted) -- a lead with no company identity isn't
            # useful downstream. The attempt is still logged to extractions
            # above; it just doesn't become a lead row.
            n_skipped_no_company += 1
            print(f"[${cost:.5f}] {row['title'][:70]!r} -> [skip-no-company] funding-related but no grounded company name")
            continue

        n_leads += 1
        summary = f"{grounded.company_name} — {grounded.funding_round} — ${grounded.amount_usd}"
        print(f"[${cost:.5f}] {row['title'][:70]!r} -> LEAD: {summary}")

        if write:
            client.table("funding_leads").upsert(
                {
                    "release_id": row["id"],
                    "extraction_id": extraction_id,
                    "company_group_key": group_key,
                    "company_name": grounded.company_name,
                    "funding_round": grounded.funding_round,
                    "amount_usd": grounded.amount_usd,
                    "investors": json.dumps(grounded.investors),
                    "sector": grounded.sector,
                    "published_at": row["published_at"],
                    "distributor": row["distributor"],
                    "grounding_failures": json.dumps(grounded.grounding_failures),
                    "fully_grounded": not grounded.grounding_failures,
                    "model_version": DEFAULT_MODEL,
                    "dedupe_key": dedupe_key(
                        group_key,
                        grounded.funding_round,
                        grounded.amount_usd,
                        row["published_at"],
                    ),
                },
                # dedupe_key (see dedupe_key() above) catches the same round
                # reported via a different release_id/url -- including a
                # translated re-release -- which release_id's own UNIQUE
                # constraint can't, and updates that existing lead instead
                # of inserting a duplicate.
                on_conflict="dedupe_key",
            ).execute()

    print(
        f"\nProcessed {len(rows)} release(s): {n_leads} lead(s)"
        f"{'' if write else ' (dry run, not written)'}, "
        f"{n_skipped_no_company} funding-related but no usable company name, "
        f"{n_not_funding} not funding-related. Estimated cost: ${total_cost:.4f}"
    )
    if not write:
        print("\n(dry run -- pass --write to log extractions and write leads)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract funding leads from unprocessed releases into funding_leads.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"Distributor to pull from (default {DEFAULT_SOURCE!r}).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max unprocessed releases to pull (default {DEFAULT_LIMIT}).")
    parser.add_argument("--write", action="store_true", help="Log extractions and write funding_leads (default: dry run).")
    args = parser.parse_args()
    run(args.source, args.limit, args.write)


if __name__ == "__main__":
    main()
