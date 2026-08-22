"""Interactive CLI for hand-labeling a quarantined gold set of releases.

Run via `python -m src.eval.label_gold`. Reads candidates from Supabase
(read-only) and appends labels to a local JSONL file — the tool never
writes back to Supabase, so the gold set stays structurally isolated from
anything the ingest/extract pipeline touches.

Sampling is stratified, not purely keyword-filtered: the primary pool is
drawn from the VC feed (`PR Newswire - Venture Capital`, funding-dense),
keyword-split into matched/unmatched. Negative controls are topped up from
the general wire feeds (`PR Newswire`, `GlobeNewswire`), which are
noise-dominated and so a more reliable source of genuine non-funding
releases than the VC feed's own leftovers. Early rounds draw mostly from
the matched pool to efficiently reach the funding target, but a fraction
of draws come from the unmatched pool throughout — both to catch
hard/unusual funding phrasing that the keyword filter missed, and to
source deliberate negative controls once the funding target is close. The
keyword filter only affects which release is shown next; the label itself
is always the human's judgment.

Teacher-assisted mode (default; disable with --no-teacher) runs the
grounded Claude extractor (src/extract/extractor.py) on each candidate
first and shows its output as a DRAFT. One keypress (Enter/a) accepts the
whole draft as-is -- fast, since most drafts need no changes. 'c' drops
into a per-field correction flow (each field defaults to the draft value,
Enter to keep it, type to override) for drafts that do need a fix; 's'
skips the candidate; 'q' saves progress and exits. Either way nothing is
written without a human looking at the draft first -- this is what makes
the saved record ground truth rather than a model dump.
"""

import argparse
import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from src.db import get_client
from src.extract.extractor import DEFAULT_MODEL, GroundedExtractionResult, extract_release

GOLD_PATH = Path(__file__).resolve().parents[2] / "data" / "gold" / "releases.jsonl"

VC_FEED_DISTRIBUTOR = "PR Newswire - Venture Capital"
NEGATIVE_CONTROL_DISTRIBUTORS = ["PR Newswire", "GlobeNewswire"]

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

    vc_response = (
        client.table("releases")
        .select("id,url,distributor,published_at,title,raw_text")
        .eq("distributor", VC_FEED_DISTRIBUTOR)
        .execute()
    )

    matched, unmatched = [], []
    for row in vc_response.data:
        if row["id"] in exclude_ids:
            continue
        pool = matched if _is_keyword_match(row["title"], row["raw_text"]) else unmatched
        pool.append(row)

    # Top up negative controls from the general (noise-dominated) feeds
    # rather than relying on the VC feed's own keyword-unmatched leftovers,
    # which can still skew borderline-funding.
    general_response = (
        client.table("releases")
        .select("id,url,distributor,published_at,title,raw_text")
        .in_("distributor", NEGATIVE_CONTROL_DISTRIBUTORS)
        .execute()
    )
    for row in general_response.data:
        if row["id"] in exclude_ids:
            continue
        if _is_keyword_match(row["title"], row["raw_text"]):
            continue
        unmatched.append(row)

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


def _prompt_amount(default: float | None) -> float | None:
    default_display = default if default is not None else "undisclosed"
    while True:
        raw = input(
            f"  amount_usd [{default_display}] "
            "(Enter to accept, e.g. 12M / 1.2B to correct, 'none' to clear): "
        ).strip()
        if raw == "":
            return default
        if raw.lower() in ("none", "null", "-"):
            return None
        try:
            return _parse_amount(raw)
        except ValueError:
            print("  couldn't parse that as a number — try again (e.g. '12M', '500000').")


def _prompt_funding_round(default: str | None) -> str:
    print(f"  funding_round [{default or '(none)'}]:")
    for i, option in enumerate(FUNDING_ROUNDS, start=1):
        marker = "  <- current" if option == default else ""
        print(f"    {i}) {option}{marker}")
    while True:
        suffix = " (Enter to accept)" if default else ""
        raw = input(f"  choose 1-{len(FUNDING_ROUNDS)}{suffix}: ").strip()
        if raw == "" and default:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(FUNDING_ROUNDS):
            choice = FUNDING_ROUNDS[int(raw) - 1]
            if choice == "other":
                custom = input("  enter custom round label: ").strip()
                return custom or "other"
            return choice
        print("  invalid choice, try again.")


def _prompt_company_name(default: str | None) -> str:
    raw = input(f"  company_name [{default or '(none)'}] (Enter to accept, or type replacement): ").strip()
    if raw == "":
        return default or ""
    return raw


def _prompt_investors(default: list[str]) -> list[str]:
    default_display = ", ".join(default) if default else "none disclosed"
    raw = input(
        f"  investors [{default_display}] (Enter to accept, or type comma-separated replacement, 'none' to clear): "
    ).strip()
    if raw == "":
        return list(default)
    if raw.lower() in ("none", "-"):
        return []
    return [i.strip() for i in raw.split(",") if i.strip()]


def _prompt_sector(default: str | None) -> str | None:
    raw = input(f"  sector [{default or '(none)'}] (Enter to accept, or type replacement, 'none' to clear): ").strip()
    if raw == "":
        return default
    if raw.lower() in ("none", "-"):
        return None
    return raw


def _prompt_fields(draft: GroundedExtractionResult | None) -> tuple[dict, list[str]]:
    """Prompt for the funding fields. If `draft` is given, each prompt shows
    the teacher extraction's value as the accept-by-default; the human must
    look at and confirm (or correct) every field before it's returned.
    Returns (fields, corrected_field_names) — the latter is empty when
    draft is None (plain manual mode, nothing to compare against).
    """
    while True:
        company_name = _prompt_company_name(draft.company_name if draft else None)
        funding_round = _prompt_funding_round(draft.funding_round if draft else None)
        amount_usd = _prompt_amount(draft.amount_usd if draft else None)
        investors = _prompt_investors(draft.investors if draft else [])
        sector = _prompt_sector(draft.sector if draft else None)

        print("\n  --- summary ---")
        print(f"  company_name:  {company_name}")
        print(f"  funding_round: {funding_round}")
        print(f"  amount_usd:    {amount_usd}")
        print(f"  investors:     {investors}")
        print(f"  sector:        {sector}")
        confirm = input("  save this? [y]es / [r]edo: ").strip().lower()
        if confirm != "r":
            fields = {
                "company_name": company_name,
                "funding_round": funding_round,
                "amount_usd": amount_usd,
                "investors": investors,
                "sector": sector,
            }
            corrected = []
            if draft is not None:
                if company_name != (draft.company_name or ""):
                    corrected.append("company_name")
                if funding_round != draft.funding_round:
                    corrected.append("funding_round")
                if amount_usd != draft.amount_usd:
                    corrected.append("amount_usd")
                if investors != draft.investors:
                    corrected.append("investors")
                if sector != draft.sector:
                    corrected.append("sector")
            return fields, corrected
        print()


def _prompt_accept_or_correct(draft: GroundedExtractionResult) -> str:
    """Fast path shown right after a teacher draft: one keypress accepts the
    whole draft (is_funding_related + all fields) exactly as shown, since
    most drafts need no changes. 'c' drops into the existing per-field
    correction flow for the drafts that do."""
    while True:
        try:
            raw = input("Accept draft as-is? [Enter/a]ccept  [c]orrect a field  [s]kip  [q]uit: ").strip().lower()
        except EOFError:
            return "q"
        if raw in ("", "a", "c", "s", "q"):
            return "a" if raw == "" else raw
        print("  invalid choice, try again.")


def _print_progress(funding_count: int, nonfunding_count: int, teacher_enabled: bool, total_cost: float) -> None:
    print(f"\nProgress: {funding_count}/{TARGET_FUNDING} funding, "
          f"{nonfunding_count} non-funding (target {MIN_NEGATIVE}-{MAX_NEGATIVE})."
          + (f" Running cost: ${total_cost:.4f}." if teacher_enabled else ""))


def _prompt_is_funding_related(draft: GroundedExtractionResult | None) -> str:
    if draft is not None:
        default = "f" if draft.is_funding_related else "n"
        label = "funding" if draft.is_funding_related else "not funding"
        prompt = f"Funding-related? [Enter = accept draft ({label})] / f / n / q: "
    else:
        default = None
        prompt = "Funding-related? [f]unding / [n]ot funding / [q]uit: "
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return "q"
    if answer == "" and default is not None:
        return default
    return answer


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


def _display_draft(draft: GroundedExtractionResult, cost: float) -> None:
    print(f"\n  [DRAFT — teacher extraction (${cost:.5f}), verify every field before saving]")
    print(f"  is_funding_related: {draft.is_funding_related}")
    if draft.is_funding_related:
        print(f"  company_name:  {draft.company_name}")
        print(f"  funding_round: {draft.funding_round}")
        print(f"  amount_usd:    {draft.amount_usd}")
        print(f"  investors:     {draft.investors}")
        print(f"  sector:        {draft.sector}")
    if draft.grounding_failures:
        print(f"  grounding flags (informational, not auto-applied): {draft.grounding_failures}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hand-label the gold set, optionally assisted by teacher-extractor drafts."
    )
    parser.add_argument(
        "--no-teacher", action="store_true",
        help="Disable teacher-assisted drafts; prompt with blank fields as before.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Model for the teacher draft extraction (default {DEFAULT_MODEL}). Ignored with --no-teacher.",
    )
    args = parser.parse_args()
    teacher_enabled = not args.no_teacher

    anthropic_client = anthropic.Anthropic() if teacher_enabled else None
    total_cost = 0.0

    seen_ids, funding_count, nonfunding_count = _load_existing(GOLD_PATH)
    matched, unmatched = _fetch_candidates(seen_ids)

    print(f"Loaded {len(matched)} keyword-matched and {len(unmatched)} unmatched candidates.")
    print(f"Resuming with {funding_count} funding / {nonfunding_count} non-funding already labeled.")
    print(f"Teacher-assisted drafts: {'on (' + args.model + ')' if teacher_enabled else 'off'}.\n")

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

        draft: GroundedExtractionResult | None = None
        if teacher_enabled:
            draft, cost = extract_release(anthropic_client, row["title"], row["raw_text"], model=args.model)
            total_cost += cost
            _display_draft(draft, cost)

            decision = _prompt_accept_or_correct(draft)
            if decision == "q":
                print("\nSaved progress and exiting.")
                break
            if decision == "s":
                print("Skipped.\n")
                continue
            if decision == "a":
                record = {
                    "release_id": row["id"],
                    "url": row["url"],
                    "distributor": row["distributor"],
                    "published_at": row["published_at"],
                    "is_funding_related": draft.is_funding_related,
                    "labeled_at": datetime.now(UTC).isoformat(),
                    "labeler_notes": "teacher-assisted: accepted",
                }
                if draft.is_funding_related:
                    record.update({
                        "company_name": draft.company_name,
                        "funding_round": draft.funding_round,
                        "amount_usd": draft.amount_usd,
                        "investors": draft.investors,
                        "sector": draft.sector,
                    })
                    funding_count += 1
                else:
                    record.update(
                        {"company_name": None, "funding_round": None, "amount_usd": None,
                         "investors": [], "sector": None}
                    )
                    nonfunding_count += 1
                _append(GOLD_PATH, record)
                _print_progress(funding_count, nonfunding_count, teacher_enabled, total_cost)
                continue
            # decision == "c": fall through to the per-field correction flow below.

        answer = _prompt_is_funding_related(draft)

        if answer == "q":
            print("\nSaved progress and exiting.")
            break

        if answer not in ("f", "n"):
            print("Unrecognized input, skipping without saving — try again.")
            pool.append(row)
            continue

        corrected_fields = []
        if draft is not None and answer != ("f" if draft.is_funding_related else "n"):
            corrected_fields.append("is_funding_related")

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
            fields, field_corrections = _prompt_fields(draft if draft is not None and draft.is_funding_related else None)
            record.update(fields)
            corrected_fields.extend(field_corrections)
            funding_count += 1
        else:
            record.update(
                {"company_name": None, "funding_round": None, "amount_usd": None,
                 "investors": [], "sector": None}
            )
            nonfunding_count += 1

        if draft is not None:
            record["labeler_notes"] = (
                f"teacher-assisted: corrected {', '.join(corrected_fields)}"
                if corrected_fields else "teacher-assisted: accepted"
            )

        _append(GOLD_PATH, record)
        _print_progress(funding_count, nonfunding_count, teacher_enabled, total_cost)


if __name__ == "__main__":
    main()
