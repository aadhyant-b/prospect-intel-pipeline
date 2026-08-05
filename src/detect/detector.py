"""Rules-based switch detector (Track C2/C3) -- no AI.

Reads the unified `releases` timeline (live + historical, one table) and
flags companies whose recent publishing behavior -- going quiet, slowing
down, or appearing on a wire they've never used before -- suggests they may
have changed PR distributor/agency.

Every scoring function takes an explicit `as_of` cutoff instead of reading
wall-clock time internally. This is deliberate, not incidental: it's what
lets src/detect/backtest.py run the exact same detector logic "as of" a past
date using only releases before that date, then check real outcomes in the
releases that came after -- validating the detector against history instead
of waiting for live drift. `python -m src.detect.detector` with no --as-of
just defaults to right now.

Run via `python -m src.detect.detector [--write] [--as-of YYYY-MM-DD] [--flag-threshold X]`.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median

from dotenv import load_dotenv

from src.db import get_client
from src.detect.wire_aliases import canonical_distributor
from src.resolve.resolver import company_group_key

load_dotenv()

logger = logging.getLogger(__name__)

# Tunable constants -- named, not inlined, so C3's feedback loop (the
# switch_predictions outcome column) can tune these against real precision
# data later instead of guessing once and leaving them.
MIN_RELEASES_FOR_BASELINE = 3   # need >=2 gaps for a median to mean anything
VOLUME_SATURATION_N = 10        # release count at which volume_weight reaches 1.0
GONE_QUIET_THRESHOLD = 3.0      # silence this many multiples of baseline_gap counts as "quiet"
CADENCE_DROP_THRESHOLD = 2.0    # recency-weighted gap this many multiples of baseline counts as "slowing"
RECENCY_HALF_LIFE_DAYS = 30.0
WIRE_CHANGE_RECENT_K = 3        # how many trailing releases count as "recent" for wire-change

W_GONE_QUIET = 1.0
W_CADENCE_DROP = 0.7
W_WIRE_CHANGE = 1.2

FLAG_THRESHOLD = 0.5


@dataclass
class FlaggedCompany:
    group_key: str
    score: float
    reasons: list[str]
    signals: dict = field(default_factory=dict)
    n_releases: int = 0
    baseline_gap_days: float | None = None
    most_recent_release_id: str | None = None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def group_releases(rows: list[dict]) -> dict[str, list[dict]]:
    """Group release rows by company_group_key(company_name_raw). Each
    grouped row keeps its id, parsed published_at, and alias-canonicalized
    distributor; groups are sorted by published_at ascending. Rows with no
    usable company signal (None or the '' "confirmed no company" sentinel)
    are dropped entirely -- nothing to group them by.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = company_group_key(row.get("company_name_raw"))
        if key is None:
            continue
        groups.setdefault(key, []).append({
            "id": row["id"],
            "published_at": _parse_timestamp(row["published_at"]),
            "distributor": canonical_distributor(row["distributor"]),
        })

    for releases in groups.values():
        releases.sort(key=lambda r: r["published_at"])
    return groups


def _gone_quiet(releases: list[dict], baseline_gap: float, as_of: datetime) -> tuple[float, str | None]:
    last = releases[-1]
    current_silence = (as_of - last["published_at"]).total_seconds() / 86400
    ratio = current_silence / baseline_gap
    if ratio <= 1:
        return 0.0, None
    score = min(ratio / GONE_QUIET_THRESHOLD, 2.0)
    reason = (
        f"Gone quiet: {current_silence:.0f} days since last release "
        f"(baseline ~{baseline_gap:.0f} days, {ratio:.1f}x normal)"
    )
    return score, reason


def _cadence_drop(gaps: list[tuple[float, float]], baseline_gap: float) -> tuple[float, str | None]:
    # gaps: list of (gap_days, age_of_later_endpoint_days)
    weights = [0.5 ** (age / RECENCY_HALF_LIFE_DAYS) for _gap, age in gaps]
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0, None
    recent_weighted_gap = sum(gap * w for (gap, _age), w in zip(gaps, weights, strict=True)) / total_weight
    ratio = recent_weighted_gap / baseline_gap
    if ratio <= 1:
        return 0.0, None
    score = min((ratio - 1) / (CADENCE_DROP_THRESHOLD - 1), 2.0)
    reason = (
        f"Cadence slowing: recent gaps ~{recent_weighted_gap:.0f} days vs "
        f"baseline ~{baseline_gap:.0f} days ({ratio:.1f}x)"
    )
    return score, reason


def _wire_change(releases: list[dict]) -> tuple[float, str | None]:
    n = len(releases)
    recent_k = min(WIRE_CHANGE_RECENT_K, n - 1)
    if recent_k < 1:
        return 0.0, None

    historical = releases[:-recent_k]
    recent = releases[-recent_k:]
    historical_distributors = {r["distributor"] for r in historical}
    recent_distributors_seq = [r["distributor"] for r in recent]
    new_distributors = set(recent_distributors_seq) - historical_distributors

    if not new_distributors:
        return 0.0, None

    new_count = sum(1 for d in recent_distributors_seq if d in new_distributors)
    score = new_count / recent_k
    reason = (
        f"Wire change: {new_count} of last {recent_k} releases moved to "
        f"{', '.join(sorted(new_distributors))} "
        f"(previously {', '.join(sorted(historical_distributors)) or 'no prior wire'} only, "
        f"{len(historical)} releases)"
    )
    return score, reason


def score_company(group_key: str, releases: list[dict], as_of: datetime) -> FlaggedCompany | None:
    """releases must already be sorted ascending by published_at and filtered
    to published_at <= as_of by the caller (see run_detector)."""
    n = len(releases)
    if n < MIN_RELEASES_FOR_BASELINE:
        return None

    gap_records = []  # (gap_days, age_of_later_endpoint_days)
    for i in range(1, n):
        gap_days = (releases[i]["published_at"] - releases[i - 1]["published_at"]).total_seconds() / 86400
        age_days = (as_of - releases[i]["published_at"]).total_seconds() / 86400
        gap_records.append((gap_days, age_days))

    baseline_gap = median(g for g, _a in gap_records)
    if baseline_gap <= 0:
        # Same-day duplicate releases collapsing the median to zero -- no
        # meaningful cadence to compare against.
        return None

    gone_quiet_score, gone_quiet_reason = _gone_quiet(releases, baseline_gap, as_of)
    cadence_drop_score, cadence_drop_reason = _cadence_drop(gap_records, baseline_gap)
    wire_change_score, wire_change_reason = _wire_change(releases)

    volume_weight = min(n / VOLUME_SATURATION_N, 1.0)
    raw = (
        W_GONE_QUIET * gone_quiet_score
        + W_CADENCE_DROP * cadence_drop_score
        + W_WIRE_CHANGE * wire_change_score
    )
    final_score = raw * volume_weight

    reasons = [r for r in (gone_quiet_reason, cadence_drop_reason, wire_change_reason) if r]

    return FlaggedCompany(
        group_key=group_key,
        score=final_score,
        reasons=reasons,
        signals={
            "gone_quiet_score": gone_quiet_score,
            "cadence_drop_score": cadence_drop_score,
            "wire_change_score": wire_change_score,
            "volume_weight": volume_weight,
            "baseline_gap_days": baseline_gap,
            "n_releases": n,
        },
        n_releases=n,
        baseline_gap_days=baseline_gap,
        most_recent_release_id=releases[-1]["id"],
    )


def run_detector(
    rows: list[dict], as_of: datetime, flag_threshold: float = FLAG_THRESHOLD
) -> list[FlaggedCompany]:
    """Pure function: takes already-fetched release rows (dicts with id,
    published_at, distributor, company_name_raw), a cutoff, and a threshold.
    Filters to published_at <= as_of before any grouping or scoring -- so
    passing a past as_of with the FULL row set (not just pre-cutoff rows)
    is safe and is exactly what src/detect/backtest.py relies on.
    """
    visible_rows = [r for r in rows if _parse_timestamp(r["published_at"]) <= as_of]
    groups = group_releases(visible_rows)

    flagged = []
    for group_key, releases in groups.items():
        result = score_company(group_key, releases, as_of)
        if result is not None and result.score > flag_threshold:
            flagged.append(result)

    flagged.sort(key=lambda f: f.score, reverse=True)
    return flagged


_FETCH_PAGE_SIZE = 1000


def fetch_releases(client) -> list[dict]:
    # PostgREST silently caps .execute() at 1000 rows server-side -- without
    # paging via .range(), the detector would score only the first 1000 of
    # what's now thousands of releases, an arbitrary and unrepresentative
    # slice that misses almost every real signal.
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (
            client.table("releases")
            .select("id,published_at,distributor,source,company_name_raw")
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


def write_predictions(client, flagged: list[FlaggedCompany], detected_at: datetime) -> int:
    if not flagged:
        return 0
    rows = [
        {
            "company_group_key": f.group_key,
            "score": f.score,
            "reasons": json.dumps(f.reasons),
            "signals": json.dumps(f.signals),
            "most_recent_release_id": f.most_recent_release_id,
            "detected_at": detected_at.isoformat(),
        }
        for f in flagged
    ]
    client.table("switch_predictions").insert(rows).execute()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the switch detector (Track C).")
    parser.add_argument("--write", action="store_true", help="Insert flagged companies into switch_predictions.")
    parser.add_argument(
        "--as-of", default=None, metavar="YYYY-MM-DD",
        help="Score as of this date using only releases up to it (default: now). "
             "For a single one-off check; src/detect/backtest.py is the tool for systematic validation.",
    )
    parser.add_argument("--flag-threshold", type=float, default=FLAG_THRESHOLD)
    args = parser.parse_args()

    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=UTC) if args.as_of else datetime.now(UTC)

    client = get_client()
    rows = fetch_releases(client)
    flagged = run_detector(rows, as_of, flag_threshold=args.flag_threshold)

    print(f"as_of={as_of.date()}  companies_flagged={len(flagged)}\n")
    for f in flagged:
        print(f"[{f.score:.2f}] {f.group_key}  (n_releases={f.n_releases})")
        for reason in f.reasons:
            print(f"    - {reason}")

    if args.write:
        inserted = write_predictions(client, flagged, as_of)
        print(f"\nWrote {inserted} rows to switch_predictions.")
    else:
        print("\n(dry run -- pass --write to log these to switch_predictions)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
