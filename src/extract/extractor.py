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
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

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


class GroundedExtractionResult(ExtractionResult):
    """ExtractionResult plus the grounding-check outcome. Never passed as
    `output_format` to the model -- Claude doesn't fill grounding_failures,
    ground_extraction() computes it afterward from the model's own output."""

    grounding_failures: list[str] = Field(default_factory=list)


# --- Grounding (Stage 1): verify every field is traceable to the source
# text before trusting it. A field that isn't found gets nulled (investors:
# dropped from the list) rather than silently passed through -- this is what
# lets src/eval/evaluator.py's cost model treat a hallucination as strictly
# worse than an honest null (grounding is what produces the honest null).
GROUNDING_FUZZY_THRESHOLD = 90.0

_AMOUNT_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "mm": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000,
}
_DOLLAR_RE = re.compile(
    r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)\s*(thousand|million|billion|k|mm?|bn|b)?\b",
    re.IGNORECASE,
)

# Funding-round keyword expected in the source text for each enum value.
# "other" is a catch-all with no fixed keyword, so it's exempt by design --
# there's nothing meaningful to check it against.
_FUNDING_ROUND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pre-seed": ("pre-seed", "pre seed"),
    "seed": ("seed",),
    "series-a": ("series a",),
    "series-b": ("series b",),
    "series-c": ("series c",),
    "series-d-plus": ("series d", "series e", "series f", "series g", "series h"),
    "debt": ("debt", "loan", "credit facility", "term note", "notes"),
    "grant": ("grant",),
    "ipo": ("ipo", "initial public offering"),
    "other": (),
}


def _fuzzy_contains(needle: str, haystack: str, threshold: float = GROUNDING_FUZZY_THRESHOLD) -> bool:
    return fuzz.partial_ratio(needle.lower(), haystack.lower()) >= threshold


def _dollar_amounts_in_text(text: str) -> list[float]:
    amounts = []
    for match in _DOLLAR_RE.finditer(text):
        number_str, suffix = match.group(1), match.group(2)
        try:
            number = float(number_str.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            number *= _AMOUNT_MULTIPLIERS.get(suffix.lower(), 1)
        amounts.append(number)
    return amounts


def _amount_grounded(amount_usd: float, text: str) -> bool:
    # 1% relative tolerance (floor $1) absorbs rounding between the model's
    # numeric value and the text's written form ("$12 million" -> 12e6 exactly,
    # but "$12.3 million" vs a source that rounds to "$12M" shouldn't fail).
    found = _dollar_amounts_in_text(text)
    return any(abs(amount_usd - f) <= max(1.0, 0.01 * max(abs(amount_usd), abs(f))) for f in found)


def _funding_round_grounded(funding_round: str, text: str) -> bool:
    keywords = _FUNDING_ROUND_KEYWORDS.get(funding_round, ())
    if not keywords:
        return True
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def ground_extraction(result: ExtractionResult, text: str) -> GroundedExtractionResult:
    """Verify every field on `result` is traceable to `text` (the exact
    title+body shown to the model). Ungrounded scalar fields are nulled;
    ungrounded investors are dropped from the list. is_funding_related is a
    judgment call, not a span, so it's exempt from grounding entirely.
    """
    failures: list[str] = []

    company_name = result.company_name
    if company_name is not None and not _fuzzy_contains(company_name, text):
        failures.append(f"company_name: {company_name!r} not found in source text")
        company_name = None

    amount_usd = result.amount_usd
    if amount_usd is not None and not _amount_grounded(amount_usd, text):
        failures.append(f"amount_usd: {amount_usd} has no matching dollar figure in source text")
        amount_usd = None

    investors = []
    for investor in result.investors:
        if _fuzzy_contains(investor, text):
            investors.append(investor)
        else:
            failures.append(f"investors: {investor!r} not found in source text")

    funding_round = result.funding_round
    if funding_round is not None and not _funding_round_grounded(funding_round, text):
        failures.append(f"funding_round: {funding_round!r} keyword not found in source text")
        funding_round = None

    return GroundedExtractionResult(
        is_funding_related=result.is_funding_related,
        company_name=company_name,
        funding_round=funding_round,
        amount_usd=amount_usd,
        investors=investors,
        sector=result.sector,
        grounding_failures=failures,
    )


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
) -> tuple[GroundedExtractionResult, float]:
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

    grounded = ground_extraction(result, f"{title}\n{body}")
    return grounded, _estimate_cost(response.usage, model)


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
