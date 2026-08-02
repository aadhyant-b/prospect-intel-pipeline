from pathlib import Path

from src.eval.evaluator import evaluate, load_jsonl

GOLD_PATH = Path(__file__).resolve().parents[1] / "data" / "gold" / "releases.jsonl"

FUNDING_ROW = {
    "release_id": "r1",
    "is_funding_related": True,
    "company_name": "Acme Inc.",
    "funding_round": "series-a",
    "amount_usd": 10_000_000.0,
    "investors": ["A Ventures", "B Capital"],
    "sector": "Fintech",
}

NON_FUNDING_ROW = {
    "release_id": "r2",
    "is_funding_related": False,
    "company_name": None,
    "funding_round": None,
    "amount_usd": None,
    "investors": [],
    "sector": None,
}


def test_perfect_prediction_scores_1():
    gold = [FUNDING_ROW, NON_FUNDING_ROW]
    predictions = [dict(FUNDING_ROW), dict(NON_FUNDING_ROW)]

    result = evaluate(gold, predictions)

    assert result.is_funding_related["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}
    assert result.is_funding_related["accuracy"] == 1.0
    assert result.fields["company_name"]["accuracy"] == 1.0
    assert result.fields["funding_round"]["accuracy"] == 1.0
    assert result.fields["amount_usd"]["accuracy"] == 1.0
    assert result.fields["sector"]["accuracy"] == 1.0
    assert result.fields["investors"]["f1"] == 1.0
    assert result.overall_score == 1.0


def test_non_funding_row_correctly_identified_excluded_from_gated_fields():
    gold = [NON_FUNDING_ROW]
    predictions = [dict(NON_FUNDING_ROW)]

    result = evaluate(gold, predictions)

    assert result.is_funding_related["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 1}
    assert result.fields["company_name"]["n"] == 0
    assert result.fields["investors"]["n"] == 0


def test_false_positive_hallucinated_funding():
    gold = [NON_FUNDING_ROW]
    predictions = [{
        "release_id": "r2",
        "is_funding_related": True,
        "company_name": "Ghost Corp",
        "funding_round": "seed",
        "amount_usd": 5_000_000.0,
        "investors": ["Nobody Ventures"],
        "sector": "Made up",
    }]

    result = evaluate(gold, predictions)

    assert result.is_funding_related["confusion"] == {"tp": 0, "fp": 1, "fn": 0, "tn": 0}
    # Gold row is non-funding, so gated fields aren't scored regardless of prediction.
    assert result.fields["company_name"]["n"] == 0


def test_false_negative_scores_gated_fields_wrong():
    gold = [FUNDING_ROW]
    predictions = [{
        "release_id": "r1",
        "is_funding_related": False,
        "company_name": None,
        "funding_round": None,
        "amount_usd": None,
        "investors": [],
        "sector": None,
    }]

    result = evaluate(gold, predictions)

    assert result.is_funding_related["confusion"] == {"tp": 0, "fp": 0, "fn": 1, "tn": 0}
    assert result.fields["company_name"]["n"] == 1
    assert result.fields["company_name"]["accuracy"] == 0.0
    assert result.fields["funding_round"]["accuracy"] == 0.0
    assert result.fields["amount_usd"]["accuracy"] == 0.0
    assert result.fields["sector"]["accuracy"] == 0.0
    # Both gold investors are unmatched false negatives.
    assert result.fields["investors"]["recall"] == 0.0


def test_fuzzy_company_name_pass_and_fail():
    gold = [
        {**FUNDING_ROW, "release_id": "r1", "company_name": "Acme Inc."},
        {**FUNDING_ROW, "release_id": "r3", "company_name": "Acme Inc."},
    ]
    predictions = [
        # Punctuation/formatting variant, token_sort_ratio ~89 -- should pass at threshold 85.
        {**FUNDING_ROW, "release_id": "r1", "company_name": "Acme, Inc"},
        # Clearly different name, should fail.
        {**FUNDING_ROW, "release_id": "r3", "company_name": "Totally Different LLC"},
    ]

    result = evaluate(gold, predictions)

    assert result.fields["company_name"]["n"] == 2
    assert result.fields["company_name"]["accuracy"] == 0.5


def test_company_name_legal_suffix_normalization():
    # Bare token_sort_ratio on "Acme Inc." vs "Acme Corporation" is only 48
    # (fails threshold 85) -- legal-suffix stripping should fix this.
    gold = [{**FUNDING_ROW, "release_id": "r1", "company_name": "Acme Inc."}]
    predictions = [{**FUNDING_ROW, "release_id": "r1", "company_name": "Acme Corporation"}]

    result = evaluate(gold, predictions)

    assert result.fields["company_name"]["accuracy"] == 1.0


def test_investors_partial_overlap_precision_recall_f1():
    gold = [{**FUNDING_ROW, "investors": ["A Ventures", "B Capital"]}]
    predictions = [{**FUNDING_ROW, "investors": ["A Ventures", "Zephyr Holdings"]}]

    result = evaluate(gold, predictions)

    assert result.fields["investors"]["precision"] == 0.5
    assert result.fields["investors"]["recall"] == 0.5
    assert result.fields["investors"]["f1"] == 0.5


def test_missing_prediction_scored_as_full_miss():
    gold = [FUNDING_ROW, NON_FUNDING_ROW]
    predictions = [dict(NON_FUNDING_ROW)]  # r1 missing entirely

    result = evaluate(gold, predictions)

    assert result.is_funding_related["confusion"]["fn"] == 1
    assert result.fields["company_name"]["accuracy"] == 0.0


def test_amount_null_handling():
    gold = [
        {**FUNDING_ROW, "release_id": "r1", "amount_usd": None},
        {**FUNDING_ROW, "release_id": "r3", "amount_usd": None},
    ]
    predictions = [
        {**FUNDING_ROW, "release_id": "r1", "amount_usd": None},  # both null -> correct
        {**FUNDING_ROW, "release_id": "r3", "amount_usd": 1_000_000.0},  # null vs number -> incorrect
    ]

    result = evaluate(gold, predictions)

    assert result.fields["amount_usd"]["accuracy"] == 0.5


def test_evaluate_runs_against_real_gold_file():
    gold = load_jsonl(GOLD_PATH)
    baseline_predictions = [
        {
            "release_id": g["release_id"],
            "is_funding_related": False,
            "company_name": None,
            "funding_round": None,
            "amount_usd": None,
            "investors": [],
            "sector": None,
        }
        for g in gold
    ]

    result = evaluate(gold, baseline_predictions)

    assert result.n_gold == len(gold)
    assert 0.0 <= result.overall_score <= 1.0
    assert result.render()
    assert result.to_json()
