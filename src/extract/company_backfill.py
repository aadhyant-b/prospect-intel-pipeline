"""Cheap Claude Haiku pass to backfill company_name_raw for historical
releases where the regex heuristic (src/ingest/historical.py) returned NULL.

Title-only, no full-text fetch -- matches Track B's "metadata only" scope
and keeps this cheap. Only ever touches rows where source != 'live': live
rows get their company name from the real extraction pipeline
(src/extract/extractor.py) once it runs against their raw_text, so
overwriting them here would be working from strictly less information.

Idempotency note: when Claude confirms a release genuinely has no single
primary company (e.g. an industry-trend piece, a person, a coalition of
multiple organizations), company_name_raw is written as '' (empty string),
not left NULL. NULL means "never attempted"; '' means "attempted, no
company found". This is what makes reruns idempotent-ish -- the query only
ever selects NULL rows, so a confirmed-no-company row is never re-sent to
Claude and re-charged. Junk rows (see _should_skip) get the same '' marker
without ever calling the API.

Run via `python -m src.extract.company_backfill [--source ...] [--limit N]`.
"""

from __future__ import annotations

import argparse
import re

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from src.db import get_client

load_dotenv()

MODEL = "claude-haiku-4-5"
# $ per million tokens: (input, output)
PRICING = {"claude-haiku-4-5": (1.00, 5.00)}

DEFAULT_LIMIT = 20

SOURCES = ("globenewswire-sitemap", "wayback-businesswire")

SYSTEM_PROMPT = """You extract the single primary company/organization name from a press release headline.

Return the company name exactly as it appears in the title (preserve legal suffixes like Inc., Corp., GmbH, Ltd.). \
Return null if the headline has no single clear company subject -- e.g. it's about a person, an event, an industry \
trend, a coalition of multiple organizations, or a wire artifact rather than a specific company's announcement.
"""

# Titles that are a single dotted token ("Gtm.js", "Gtm.start") are
# analytics/tracking-script captures that leaked into the Wayback CDX crawl,
# not real headlines -- a real headline always has spaces. Skip without
# spending an API call.
_JUNK_TITLE_RE = re.compile(r"^\S+\.\S+$")
# Exact placeholder historical.py writes when a Wayback URL has no slug.
_PLACEHOLDER_TITLE = "(untitled Business Wire release)"


class CompanyNameResult(BaseModel):
    company_name: str | None


def _should_skip(title: str) -> bool:
    return bool(_JUNK_TITLE_RE.match(title.strip())) or title == _PLACEHOLDER_TITLE


def _estimate_cost(usage) -> float:
    in_price, out_price = PRICING[MODEL]
    return (usage.input_tokens / 1_000_000) * in_price + (usage.output_tokens / 1_000_000) * out_price


def extract_company_name(client: anthropic.Anthropic, title: str) -> tuple[str | None, float]:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": title}],
        output_format=CompanyNameResult,
    )
    cost = _estimate_cost(response.usage)
    if response.stop_reason == "refusal":
        return None, cost
    return response.parsed_output.company_name, cost


def _fetch_null_rows(source: str | None, limit: int) -> list[dict]:
    query = get_client().table("releases").select("id,title,source").is_("company_name_raw", "null")
    query = query.eq("source", source) if source else query.neq("source", "live")
    return query.limit(limit).execute().data


def run(source: str | None, limit: int) -> None:
    anthropic_client = anthropic.Anthropic()
    db = get_client()
    rows = _fetch_null_rows(source, limit)

    total_cost = 0.0
    filled = 0
    junk_skipped = 0

    for row in rows:
        title = (row["title"] or "").strip()

        if not title or _should_skip(title):
            junk_skipped += 1
            db.table("releases").update({"company_name_raw": ""}).eq("id", row["id"]).execute()
            print(f"[skip-junk] {title!r}")
            continue

        company_name, cost = extract_company_name(anthropic_client, title)
        total_cost += cost
        db.table("releases").update({"company_name_raw": company_name or ""}).eq("id", row["id"]).execute()
        if company_name:
            filled += 1
        print(f"[${cost:.6f}] {title[:70]!r} -> {company_name!r}")

    no_company = len(rows) - filled - junk_skipped
    print(
        f"\nProcessed {len(rows)} rows: filled={filled} confirmed_no_company={no_company} "
        f"junk_skipped={junk_skipped}. Estimated cost: ${total_cost:.4f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill company_name_raw via Claude Haiku (title-only).")
    parser.add_argument("--source", choices=SOURCES, default=None, help="Limit to one source; default runs both.")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Max rows to process (default {DEFAULT_LIMIT}).",
    )
    args = parser.parse_args()
    run(args.source, args.limit)


if __name__ == "__main__":
    main()
