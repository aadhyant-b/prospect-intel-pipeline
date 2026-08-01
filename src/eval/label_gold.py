"""Interactive CLI for hand-labeling a quarantined gold set of releases.

Run via `python -m src.eval.label_gold`. Reads candidates from Supabase
(read-only) and appends labels to a local JSONL file — the tool never
writes back to Supabase, so the gold set stays structurally isolated from
anything the ingest/extract pipeline touches.

Sampling is stratified, not purely keyword-filtered: releases are split
into a keyword-matched pool (likely funding announcements) and an
unmatched pool (everything else). Early rounds draw mostly from the
matched pool to efficiently reach the funding target, but a fraction of
draws come from the unmatched pool throughout — both to catch
hard/unusual funding phrasing that the keyword filter missed, and to
source deliberate negative controls (confirmed non-funding releases) once
the funding target is close. The keyword filter only affects which
release is shown next; the label itself is always the human's judgment.
"""

import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path

from src.db import get_client

GOLD_PATH = Path(__file__).resolve().parents[2] / "data" / "gold" / "releases.jsonl"

TARGET_FUNDING = 80
MIN_NEGATIVE = 15
MAX_NEGATIVE = 20
# During phase A (still short of the funding target), the fraction of
# draws taken from the matched pool rather than the unmatched pool.
MATCHED_DRAW_RATIO = 0.8

FUNDING_KEYWORDS = [
    "raises", "raised", "raising",
    "series a", "series b", "series c", "series d", "series e",
    "seed round", "seed funding", "seed extension",
    "pre-seed", "pre seed",
    "funding round", "closes funding", "closes financing",
    "secures funding", "secures investment", "secures financing",
    "investment from", "venture capital", "venture round",
    "led by", "equity financing", "debt financing",
    "million in funding", "billion in funding",
    "oversubscribed round", "growth equity", "term loan",
]

FUNDING_ROUNDS = [
    "pre-seed", "seed", "series-a", "series-b", "series-c",
    "series-d-plus", "debt", "grant", "ipo", "other",
]


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _is_keyword_match(title: str, raw_text: str) -> bool:
    haystack = f"{title} {raw_text}".lower()
    return any(kw in haystack for kw in FUNDING_KEYWORDS)


def _load_existing(path: Path) -> tuple[set[str], int, int]:
    if not path.exists():
        return set(), 0, 0

    seen_ids: set[str] = set()
    funding_count = 0
    nonfunding_count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        seen_ids.add(record["release_id"])
        if record["is_funding_related"]:
            funding_count += 1
        else:
            nonfunding_count += 1
    return seen_ids, funding_count, nonfunding_count


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _fetch_candidates(exclude_ids: set[str]) -> tuple[list[dict], list[dict]]:
    client = get_client()
    response = (
        client.table("releases")
        .select("id,url,distributor,published_at,title,raw_text")
        .execute()
    )

    matched, unmatched = [], []
    for row in response.data:
        if row["id"] in exclude_ids:
            continue
        pool = matched if _is_keyword_match(row["title"], row["raw_text"]) else unmatched
        pool.append(row)

    random.shuffle(matched)
    random.shuffle(unmatched)
    return matched, unmatched


def _pick_pool(
    matched: list[dict],
    unmatched: list[dict],
    funding_count: int,
    nonfunding_count: int,
) -> list[dict] | None:
    """Decide which pool to draw the next candidate from."""
    if funding_count < TARGET_FUNDING:
        prefer_matched = nonfunding_count >= MAX_NEGATIVE or random.random() < MATCHED_DRAW_RATIO
        primary, fallback = (matched, unmatched) if prefer_matched else (unmatched, matched)
    else:
        # Funding target met — only chasing negative controls now.
        primary, fallback = unmatched, matched

    if primary:
        return primary
    if fallback:
        return fallback
    return None


def _parse_amount(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    s = raw.lower().replace(",", "").replace("$", "").strip()
    multiplier = 1
    for suffix, mult in (("billion", 1_000_000_000), ("bn", 1_000_000_000), ("b", 1_000_000_000),
                         ("million", 1_000_000), ("mm", 1_000_000), ("m", 1_000_000),
                         ("thousand", 1_000), ("k", 1_000)):
        if s.endswith(suffix):
            multiplier = mult
            s = s[: -len(suffix)].strip()
            break
    return float(s) * multiplier


def _prompt_amount() -> float | None:
    while True:
        raw = input("  amount_usd (e.g. 12M, 1.2B, or blank if undisclosed): ").strip()
        try:
            return _parse_amount(raw)
        except ValueError:
            print("  couldn't parse that as a number — try again (e.g. '12M', '500000', or blank).")


def _prompt_funding_round() -> str:
    print("  funding_round:")
    for i, option in enumerate(FUNDING_ROUNDS, start=1):
        print(f"    {i}) {option}")
    while True:
        raw = input(f"  choose 1-{len(FUNDING_ROUNDS)}: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(FUNDING_ROUNDS):
            choice = FUNDING_ROUNDS[int(raw) - 1]
            if choice == "other":
                custom = input("  enter custom round label: ").strip()
                return custom or "other"
            return choice
        print("  invalid choice, try again.")


def _prompt_fields() -> dict:
    while True:
        company_name = input("  company_name: ").strip()
        funding_round = _prompt_funding_round()
        amount_usd = _prompt_amount()
        investors_raw = input("  investors (comma-separated, blank if none disclosed): ").strip()
        investors = [i.strip() for i in investors_raw.split(",") if i.strip()]
        sector = input("  sector (blank if unclear): ").strip() or None

        print("\n  --- summary ---")
        print(f"  company_name:  {company_name}")
        print(f"  funding_round: {funding_round}")
        print(f"  amount_usd:    {amount_usd}")
        print(f"  investors:     {investors}")
        print(f"  sector:        {sector}")
        confirm = input("  save this? [y]es / [r]edo: ").strip().lower()
        if confirm != "r":
            return {
                "company_name": company_name,
                "funding_round": funding_round,
                "amount_usd": amount_usd,
                "investors": investors,
                "sector": sector,
            }
        print()


def _display_release(row: dict, from_matched_pool: bool) -> None:
    tag = "keyword-matched candidate" if from_matched_pool else "unmatched candidate"
    print("\n" + "=" * 78)
    print(f"[{tag}]  {row['distributor']}  |  {row['published_at']}")
    print(row["title"])
    print(row["url"])
    print("-" * 78)
    body = _strip_html(row["raw_text"])
    print(body[:2000] + ("..." if len(body) > 2000 else ""))
    print("=" * 78)


def main() -> None:
    seen_ids, funding_count, nonfunding_count = _load_existing(GOLD_PATH)
    matched, unmatched = _fetch_candidates(seen_ids)

    print(f"Loaded {len(matched)} keyword-matched and {len(unmatched)} unmatched candidates.")
    print(f"Resuming with {funding_count} funding / {nonfunding_count} non-funding already labeled.\n")

    while True:
        if funding_count >= TARGET_FUNDING and nonfunding_count >= MIN_NEGATIVE:
            print(f"\nTarget reached: {funding_count} funding, {nonfunding_count} non-funding. Done.")
            break

        pool = _pick_pool(matched, unmatched, funding_count, nonfunding_count)
        if pool is None:
            print("\nNo more candidates available from Supabase. Stopping short of target.")
            break

        row = pool.pop()
        from_matched_pool = pool is matched
        _display_release(row, from_matched_pool)

        try:
            answer = input("Funding-related? [f]unding / [n]ot funding / [q]uit: ").strip().lower()
        except EOFError:
            answer = "q"

        if answer == "q":
            print("\nSaved progress and exiting.")
            break

        if answer not in ("f", "n"):
            print("Unrecognized input, skipping without saving — try again.")
            pool.append(row)
            continue

        record = {
            "release_id": row["id"],
            "url": row["url"],
            "distributor": row["distributor"],
            "published_at": row["published_at"],
            "is_funding_related": answer == "f",
            "labeled_at": datetime.now(UTC).isoformat(),
            "labeler_notes": None,
        }

        if answer == "f":
            record.update(_prompt_fields())
            funding_count += 1
        else:
            record.update(
                {"company_name": None, "funding_round": None, "amount_usd": None,
                 "investors": [], "sector": None}
            )
            nonfunding_count += 1

        _append(GOLD_PATH, record)
        print(f"\nProgress: {funding_count}/{TARGET_FUNDING} funding, "
              f"{nonfunding_count} non-funding (target {MIN_NEGATIVE}-{MAX_NEGATIVE}).")


if __name__ == "__main__":
    main()
