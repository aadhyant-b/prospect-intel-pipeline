"""Scores extraction-model predictions against the hand-labeled gold set.

Predictions use the same schema as gold records (keyed by ``release_id``):
``is_funding_related``, ``company_name``, ``funding_round``, ``amount_usd``,
``investors``, ``sector``. ``is_funding_related`` is scored on every gold
row; the other fields are gated — scored only on rows where gold says
``is_funding_related`` is true, since those are the only rows where a
correct value even exists to compare against.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

DEFAULT_FUZZY_THRESHOLD = 85.0

# Longest-first so e.g. "Corporation" is stripped before a shorter overlapping
# suffix could partially match it.
_LEGAL_SUFFIXES = [
    "corporation", "incorporated", "company",
    "l.l.c.", "l.l.c", "llc", "plc",
    "corp.", "corp", "inc.", "inc",
    "ltd.", "ltd", "co.", "co",
]
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\.?$",
    re.IGNORECASE,
)


def load_jsonl(path: Path) -> list[dict]:
    records = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _normalize_company_name(name: str) -> str:
    normalized = name.strip().lower()
    while True:
        stripped = _LEGAL_SUFFIX_RE.sub("", normalized).strip()
        if stripped == normalized:
            return normalized
        normalized = stripped


def _fuzzy_match(a: str | None, b: str | None, threshold: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return fuzz.token_sort_ratio(a, b) >= threshold


def _company_name_match(a: str | None, b: str | None, threshold: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return _fuzzy_match(_normalize_company_name(a), _normalize_company_name(b), threshold)


def _exact_match(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.strip().lower(), b.strip().lower()
    return a == b


def _match_investors(gold: list[str], pred: list[str], threshold: float) -> tuple[int, int, int]:
    """Greedy fuzzy set matching. Returns (tp, fp, fn)."""
    remaining_gold = list(gold)
    tp = 0
    for p in pred:
        best_idx, best_score = None, -1.0
        for i, g in enumerate(remaining_gold):
            score = fuzz.token_sort_ratio(p, g)
            if score > best_score:
                best_idx, best_score = i, score
        if best_idx is not None and best_score >= threshold:
            remaining_gold.pop(best_idx)
            tp += 1
    fp = len(pred) - tp
    fn = len(remaining_gold)
    return tp, fp, fn


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _prf1(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


@dataclass
class EvalResult:
    n_gold: int
    n_funding_related: int
    is_funding_related: dict[str, Any]
    fields: dict[str, dict[str, Any]]
    overall_score: float
    fuzzy_threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_gold": self.n_gold,
            "n_funding_related": self.n_funding_related,
            "is_funding_related": self.is_funding_related,
            "fields": self.fields,
            "overall_score": self.overall_score,
            "fuzzy_threshold": self.fuzzy_threshold,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def render(self) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("EXTRACTION EVAL SCORECARD")
        lines.append("=" * 60)
        lines.append(f"n_gold={self.n_gold}  n_funding_related={self.n_funding_related}  "
                      f"fuzzy_threshold={self.fuzzy_threshold}")
        lines.append("")

        c = self.is_funding_related["confusion"]
        lines.append("is_funding_related")
        lines.append(f"  confusion: tp={c['tp']} fp={c['fp']} fn={c['fn']} tn={c['tn']}")
        lines.append(f"  accuracy={self.is_funding_related['accuracy']:.3f}  "
                      f"precision={self.is_funding_related['precision']:.3f}  "
                      f"recall={self.is_funding_related['recall']:.3f}  "
                      f"f1={self.is_funding_related['f1']:.3f}")
        lines.append("")

        lines.append(f"{'field':<16}{'metric':<12}{'value':<10}{'n'}")
        lines.append("-" * 48)
        for name, metrics in self.fields.items():
            n = metrics.get("n", "")
            printed_n = False
            for key, value in metrics.items():
                if key == "n":
                    continue
                lines.append(f"{name:<16}{key:<12}{value:<10.3f}{'' if printed_n else n}")
                printed_n = True
        lines.append("")
        lines.append(f"OVERALL SCORE: {self.overall_score:.3f}")
        lines.append("=" * 60)
        return "\n".join(lines)


def evaluate(
    gold: list[dict],
    predictions: list[dict],
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> EvalResult:
    pred_by_id = {p["release_id"]: p for p in predictions}

    known_ids = {g["release_id"] for g in gold}
    unknown_pred_ids = set(pred_by_id) - known_ids
    if unknown_pred_ids:
        print(
            f"warning: ignoring {len(unknown_pred_ids)} prediction(s) with release_id "
            f"not present in gold: {sorted(unknown_pred_ids)[:5]}",
            file=sys.stderr,
        )

    tp = fp = fn = tn = 0
    investor_tp = investor_fp = investor_fn = 0
    field_correct = {"company_name": 0, "funding_round": 0, "amount_usd": 0, "sector": 0}
    n_gated = 0

    for g in gold:
        p = pred_by_id.get(g["release_id"], {})
        gold_related = bool(g["is_funding_related"])
        pred_related = bool(p.get("is_funding_related", False))

        if gold_related and pred_related:
            tp += 1
        elif gold_related and not pred_related:
            fn += 1
        elif not gold_related and pred_related:
            fp += 1
        else:
            tn += 1

        if not gold_related:
            continue

        n_gated += 1

        if not pred_related:
            # Model missed funding entirely: every gated field is wrong for this row.
            g_investors = g.get("investors") or []
            investor_fn += len(g_investors)
            continue

        if _company_name_match(g.get("company_name"), p.get("company_name"), fuzzy_threshold):
            field_correct["company_name"] += 1
        if _exact_match(g.get("funding_round"), p.get("funding_round")):
            field_correct["funding_round"] += 1
        if _exact_match(g.get("amount_usd"), p.get("amount_usd")):
            field_correct["amount_usd"] += 1
        if _fuzzy_match(g.get("sector"), p.get("sector"), fuzzy_threshold):
            field_correct["sector"] += 1

        row_tp, row_fp, row_fn = _match_investors(
            g.get("investors") or [], p.get("investors") or [], fuzzy_threshold
        )
        investor_tp += row_tp
        investor_fp += row_fp
        investor_fn += row_fn

    ifr_prf1 = _prf1(tp, fp, fn)
    is_funding_related = {
        "accuracy": _safe_div(tp + tn, tp + tn + fp + fn),
        **ifr_prf1,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }

    fields = {
        "company_name": {"accuracy": _safe_div(field_correct["company_name"], n_gated), "n": n_gated},
        "funding_round": {"accuracy": _safe_div(field_correct["funding_round"], n_gated), "n": n_gated},
        "amount_usd": {"accuracy": _safe_div(field_correct["amount_usd"], n_gated), "n": n_gated},
        "sector": {"accuracy": _safe_div(field_correct["sector"], n_gated), "n": n_gated},
        "investors": {**_prf1(investor_tp, investor_fp, investor_fn), "n": n_gated},
    }

    component_scores = [is_funding_related["f1"]]
    for name in ("company_name", "funding_round", "amount_usd", "sector"):
        component_scores.append(fields[name]["accuracy"])
    component_scores.append(fields["investors"]["f1"])
    overall_score = sum(component_scores) / len(component_scores)

    return EvalResult(
        n_gold=len(gold),
        n_funding_related=sum(1 for g in gold if g["is_funding_related"]),
        is_funding_related=is_funding_related,
        fields=fields,
        overall_score=overall_score,
        fuzzy_threshold=fuzzy_threshold,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Score predictions against the gold set.")
    parser.add_argument("gold_path", type=Path)
    parser.add_argument("predictions_path", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="Write JSON result to this path.")
    parser.add_argument("--fuzzy-threshold", type=float, default=DEFAULT_FUZZY_THRESHOLD)
    args = parser.parse_args()

    gold = load_jsonl(args.gold_path)
    predictions = load_jsonl(args.predictions_path)
    result = evaluate(gold, predictions, fuzzy_threshold=args.fuzzy_threshold)

    print(result.render())
    if args.out:
        args.out.write_text(result.to_json())
        print(f"\nWrote JSON result to {args.out}")


if __name__ == "__main__":
    main()
