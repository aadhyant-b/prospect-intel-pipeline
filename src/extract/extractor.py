"""Claude-based "teacher" extractor: pulls structured funding fields out of a
press release's title + body.

Output uses the exact field shape as the hand-labeled gold set
(data/gold/releases.jsonl, documented in data/gold/README.md) so a run's
predictions can be scored directly by src/eval/evaluator.py with no
field-mapping glue, and later used as training data for a distilled model.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Literal

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from src.db import get_client

load_dotenv()

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_LIMIT = 10
MAX_BODY_CHARS = 6000

# $ per million tokens: (input, output)
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
}

FundingRound = Literal[
    "pre-seed", "seed", "series-a", "series-b", "series-c",
    "series-d-plus", "debt", "grant", "ipo", "other",
]

SYSTEM_PROMPT = """You extract structured funding data from company press releases.

Determine is_funding_related first: true only if the release announces a company \
raising funding (equity, debt, grant, or IPO proceeds). Routine business news, \
earnings reports, product launches, litigation notices, and partnership \
announcements are NOT funding-related, even if they mention money.

If is_funding_related is true, fill in the other fields from what the release \
states. If is_funding_related is false, leave company_name, funding_round, and \
amount_usd null and investors empty — do not invent values.

- funding_round: pick the closest match from the fixed set (pre-seed, seed, \
series-a, series-b, series-c, series-d-plus, debt, grant, ipo, other). Use \
"other" if none fit or the round type isn't stated.
- amount_usd: the disclosed raise amount in US dollars as a number, or null if \
undisclosed.
- investors: the named investors/lead investors, or an empty list if none are \
named.
- sector: a short description of the company's industry/category, or null if \
unclear.
"""


class ExtractionResult(BaseModel):
    is_funding_related: bool
    company_name: str | None
    funding_round: FundingRound | None
    amount_usd: float | None
    investors: list[str]
    sector: str | None


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _estimate_cost(usage, model: str) -> float:
    in_price, out_price = PRICING[model]
    input_tokens = usage.input_tokens + getattr(usage, "cache_read_input_tokens", 0) or 0
    return (input_tokens / 1_000_000) * in_price + (usage.output_tokens / 1_000_000) * out_price


def extract_release(
    client: anthropic.Anthropic,
    title: str,
    raw_text: str,
    model: str = DEFAULT_MODEL,
) -> tuple[ExtractionResult, float]:
    body = _strip_html(raw_text)[:MAX_BODY_CHARS]
    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Title: {title}\n\nBody:\n{body}"}],
        output_format=ExtractionResult,
    )

    if response.stop_reason == "refusal":
        result = ExtractionResult(
            is_funding_related=False, company_name=None, funding_round=None,
            amount_usd=None, investors=[], sector=None,
        )
    else:
        result = response.parsed_output

    return result, _estimate_cost(response.usage, model)


def _fetch_releases(limit: int, distributor: str | None) -> list[dict]:
    query = get_client().table("releases").select(
        "id,url,distributor,published_at,title,raw_text"
    )
    if distributor:
        query = query.eq("distributor", distributor)
    response = query.order("published_at", desc=True).limit(limit).execute()
    return response.data


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Claude teacher extractor over releases.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                         help=f"Max releases to process (default {DEFAULT_LIMIT}).")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=list(PRICING))
    parser.add_argument("--distributor", default=None)
    parser.add_argument("--out", type=Path, default=Path("predictions.jsonl"))
    args = parser.parse_args()

    client = anthropic.Anthropic()
    releases = _fetch_releases(args.limit, args.distributor)

    total_cost = 0.0
    funding_count = 0
    records = []

    for release in releases:
        result, cost = extract_release(client, release["title"], release["raw_text"], model=args.model)
        total_cost += cost
        if result.is_funding_related:
            funding_count += 1
            summary = f"{result.company_name} — {result.funding_round} — ${result.amount_usd}"
        else:
            summary = "not funding-related"
        print(f"[${cost:.5f}] {release['title'][:70]!r} -> {summary}")

        records.append({"release_id": release["id"], **result.model_dump()})

    args.out.write_text("\n".join(json.dumps(r) for r in records) + "\n" if records else "")

    print(
        f"\nProcessed {len(records)} releases ({funding_count} funding-related). "
        f"Estimated cost: ${total_cost:.4f}. Wrote {args.out}"
    )


if __name__ == "__main__":
    main()
