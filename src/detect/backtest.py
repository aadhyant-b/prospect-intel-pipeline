"""Backtest harness for the switch detector (Track C, validation).

Runs the detector "as of" a past cutoff date using only releases before it
(src/detect/detector.py::run_detector already filters on `as_of`, which is
exactly why that parameter exists rather than reading wall-clock time), then
checks the releases that actually happened AFTER the cutoff to see whether
each flagged company really did show up on a new wire. This gives real
precision/recall against history, without waiting for live outcomes or
depending on Track B2's not-yet-built retrospective mining.

Ground truth per company: did any release after the cutoff (within an
optional validation window) land on a canonical distributor never seen in
that company's PRE-cutoff history? A company is excluded from the confusion
matrix (not counted as a negative) when there isn't enough post-cutoff data
to know one way or the other -- "we don't know if they went quiet forever or
just haven't posted since" is a real state, distinct from "confirmed no
switch".

Run via `python -m src.detect.backtest --cutoff YYYY-MM-DD [--validation-days N] [--flag-threshold X] [--write]`.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.db import get_client
from src.detect.detector import (
    FLAG_THRESHOLD,
    MIN_RELEASES_FOR_BASELINE,
    FlaggedCompany,
    fetch_releases,
    group_releases,
    run_detector,
)

logger = logging.getLogger(__name__)

# Ground-truth verdicts for a company as of a cutoff:
#   "confirmed_switch"    -- a post-cutoff release landed on a new wire
#   "no_switch_observed"  -- post-cutoff releases exist, all on known wires
#   "unknown"             -- no post-cutoff releases at all (nothing to check)
#   "ineligible"           -- fewer than MIN_RELEASES_FOR_BASELINE releases
#                             before the cutoff (detector could never have
#                             scored them either)


@dataclass
class BacktestResult:
    cutoff: datetime
    flagged: list[FlaggedCompany]
    outcomes: dict[str, str]
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)


def _ground_truth(all_releases_for_group: list[dict], cutoff: datetime, validation_end: datetime | None) -> str:
    """all_releases_for_group must be sorted ascending, distributor already
    canonicalized (i.e. straight from group_releases())."""
    historical = [r for r in all_releases_for_group if r["published_at"] <= cutoff]
    if len(historical) < MIN_RELEASES_FOR_BASELINE:
        return "ineligible"

    historical_distributors = {r["distributor"] for r in historical}
    end = validation_end or datetime.max.replace(tzinfo=UTC)
    future = [r for r in all_releases_for_group if cutoff < r["published_at"] <= end]
    if not future:
        return "unknown"
    if any(r["distributor"] not in historical_distributors for r in future):
        return "confirmed_switch"
    return "no_switch_observed"


def run_backtest(
    rows: list[dict],
    cutoff: datetime,
    validation_days: int | None = None,
    flag_threshold: float = FLAG_THRESHOLD,
) -> BacktestResult:
    validation_end = cutoff + timedelta(days=validation_days) if validation_days else None

    flagged = run_detector(rows, as_of=cutoff, flag_threshold=flag_threshold)
    flagged_keys = {f.group_key for f in flagged}

    all_groups = group_releases(rows)  # over ALL rows, past and future of cutoff
    outcomes = {key: _ground_truth(releases, cutoff, validation_end) for key, releases in all_groups.items()}

    tp = fp = fn = tn = 0
    for group_key, outcome in outcomes.items():
        if outcome in ("ineligible", "unknown"):
            continue
        switched = outcome == "confirmed_switch"
        was_flagged = group_key in flagged_keys
        if was_flagged and switched:
            tp += 1
        elif was_flagged and not switched:
            fp += 1
        elif not was_flagged and switched:
            fn += 1
        else:
            tn += 1

    return BacktestResult(cutoff=cutoff, flagged=flagged, outcomes=outcomes, tp=tp, fp=fp, fn=fn, tn=tn)


_OUTCOME_MAP = {
    "confirmed_switch": "confirmed_switch",
    "no_switch_observed": "false_positive",
    "unknown": "unknown",
}


def write_backtest_predictions(client, result: BacktestResult) -> int:
    if not result.flagged:
        return 0
    now = datetime.now(UTC)
    rows = [
        {
            "company_group_key": f.group_key,
            "score": f.score,
            "reasons": json.dumps(f.reasons),
            "signals": json.dumps(f.signals),
            "most_recent_release_id": f.most_recent_release_id,
            "detected_at": result.cutoff.isoformat(),
            "outcome": _OUTCOME_MAP.get(result.outcomes.get(f.group_key, "unknown"), "unknown"),
            "outcome_notes": "backtest",
            "outcome_recorded_at": now.isoformat(),
        }
        for f in result.flagged
    ]
    client.table("switch_predictions").insert(rows).execute()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the switch detector against real history.")
    parser.add_argument("--cutoff", required=True, metavar="YYYY-MM-DD")
    parser.add_argument(
        "--validation-days", type=int, default=None,
        help="Only check this many days after the cutoff for a switch (default: unbounded -- use all available future data).",
    )
    parser.add_argument("--flag-threshold", type=float, default=FLAG_THRESHOLD)
    parser.add_argument("--write", action="store_true", help="Log flagged predictions + their known outcomes to switch_predictions.")
    args = parser.parse_args()

    cutoff = datetime.fromisoformat(args.cutoff).replace(tzinfo=UTC)

    client = get_client()
    rows = fetch_releases(client)
    result = run_backtest(rows, cutoff, validation_days=args.validation_days, flag_threshold=args.flag_threshold)

    print(f"cutoff={cutoff.date()}  validation_days={args.validation_days or 'unbounded'}  "
          f"companies_flagged={len(result.flagged)}\n")
    for f in result.flagged:
        outcome = result.outcomes.get(f.group_key, "unknown")
        print(f"[{f.score:.2f}] {f.group_key}  ->  {outcome}")
        for reason in f.reasons:
            print(f"    - {reason}")

    print(
        f"\nConfusion matrix (companies with a known post-cutoff outcome only): "
        f"TP={result.tp} FP={result.fp} FN={result.fn} TN={result.tn}"
    )
    print(
        f"precision={result.precision if result.precision is None else f'{result.precision:.2f}'}  "
        f"recall={result.recall if result.recall is None else f'{result.recall:.2f}'}  "
        f"f1={result.f1 if result.f1 is None else f'{result.f1:.2f}'}"
    )

    if args.write:
        inserted = write_backtest_predictions(client, result)
        print(f"\nWrote {inserted} rows to switch_predictions (with known outcomes).")
    else:
        print("\n(dry run -- pass --write to log these, with outcomes, to switch_predictions)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
