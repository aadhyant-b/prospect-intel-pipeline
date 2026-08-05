from pathlib import Path

import pytest
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
    assert result.cost_weighted_score == 1.0
    assert result.total_cost == 0.0


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
    # Hand-computed cost: every gated field is an abstention (pred says
    # not-funding-related, so nothing was even attempted), costing
    # weight*NULL_ABSTENTION_MULTIPLIER(1.0) each: company_name(3)+
    # funding_round(1)+amount_usd(2)+sector(1) = 7.0, plus both gold
    # investors missed at 1.5*1.0 each = 3.0 -> total_cost=10.0.
    # max_cost is the same fields' FALSE_CLAIM tier (weight*3.0 each):
    # (3+1+2+1)*3=21.0, plus 2 investors*1.5*3=9.0 -> max_cost=30.0.
    # cost_weighted_score = 1 - 10/30 = 2/3.
    assert result.total_cost == pytest.approx(10.0)
    assert result.max_cost == pytest.approx(30.0)
    assert result.cost_weighted_score == pytest.approx(2 / 3)


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


# --- Cost-weighted evaluation (Stage 2) ------------------------------

def test_false_claim_costs_more_than_hand_computed():
    # Hand-computed hallucination case: company_name and amount_usd are both
    # confidently WRONG (non-null, mismatched) rather than null; funding_round
    # and sector are correct; investors has one real match, one hallucinated
    # extra, and one missed gold investor.
    gold = [FUNDING_ROW]  # company_name="Acme Inc.", amount_usd=10_000_000.0,
    #                        funding_round="series-a", sector="Fintech",
    #                        investors=["A Ventures", "B Capital"]
    predictions = [{
        "release_id": "r1",
        "is_funding_related": True,
        "company_name": "Totally Wrong Co",       # false claim (weight 3.0)
        "funding_round": "series-a",                # correct
        "amount_usd": 999.0,                         # false claim (weight 2.0)
        "investors": ["A Ventures", "Fake Ventures"],  # 1 tp, 1 fp, 1 fn (weight 1.5)
        "sector": "Fintech",                         # correct
    }]

    result = evaluate(gold, predictions)

    # company_name: weight 3.0 * FALSE_CLAIM_MULTIPLIER 3.0 = 9.0
    # amount_usd:   weight 2.0 * FALSE_CLAIM_MULTIPLIER 3.0 = 6.0
    # investors:    1 fp * 1.5 * 3.0 (false claim) + 1 fn * 1.5 * 1.0 (abstention) = 4.5 + 1.5 = 6.0
    # funding_round, sector: matched, 0 each
    # total_cost = 9.0 + 6.0 + 6.0 = 21.0
    # max_cost = (3+1+2+1)*3.0 + 2 investors*1.5*3.0 = 21.0 + 9.0 = 30.0
    # cost_weighted_score = 1 - 21/30 = 0.3
    assert result.total_cost == pytest.approx(21.0)
    assert result.max_cost == pytest.approx(30.0)
    assert result.cost_weighted_score == pytest.approx(0.3)


def test_null_abstention_cheaper_than_false_claim_same_field():
    # The core Stage 1/2 interaction: grounding turns a would-be hallucination
    # into a null. That null must score cheaper than the un-grounded false
    # claim would have -- otherwise grounding wouldn't be incentivized.
    gold = [FUNDING_ROW]
    false_claim_pred = [{**FUNDING_ROW, "company_name": "Fabricated Corp"}]
    grounded_null_pred = [{**FUNDING_ROW, "company_name": None}]

    false_claim_result = evaluate(gold, false_claim_pred)
    grounded_null_result = evaluate(gold, grounded_null_pred)

    assert false_claim_result.total_cost > grounded_null_result.total_cost
    assert false_claim_result.cost_weighted_score < grounded_null_result.cost_weighted_score
    # Exact ratio: false claim costs FALSE_CLAIM_MULTIPLIER(3.0) / NULL_ABSTENTION_MULTIPLIER(1.0) = 3x.
    assert false_claim_result.total_cost == pytest.approx(3 * grounded_null_result.total_cost)


def test_company_name_error_weighted_above_sector_error():
    # Same magnitude of error (one wrong field, rest correct), different
    # field -- company_name should cost more than sector, matching the
    # "company_name errors weighted highest" design intent.
    gold = [FUNDING_ROW]
    wrong_company = [{**FUNDING_ROW, "company_name": "Fabricated Corp"}]
    wrong_sector = [{**FUNDING_ROW, "sector": "Completely Unrelated Industry"}]

    company_result = evaluate(gold, wrong_company)
    sector_result = evaluate(gold, wrong_sector)

    assert company_result.total_cost > sector_result.total_cost
    assert company_result.cost_weighted_score < sector_result.cost_weighted_score


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
