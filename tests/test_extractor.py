from src.extract.extractor import (
    ExtractionResult,
    _amount_grounded,
    _dollar_amounts_in_text,
    _strip_html,
    ground_extraction,
)


def test_extraction_result_accepts_funding_record():
    result = ExtractionResult(
        is_funding_related=True,
        company_name="Acme Inc.",
        funding_round="series-a",
        amount_usd=10_000_000.0,
        investors=["A Ventures"],
        sector="Fintech",
    )
    assert result.funding_round == "series-a"


def test_extraction_result_accepts_null_non_funding_record():
    result = ExtractionResult(
        is_funding_related=False,
        company_name=None,
        funding_round=None,
        amount_usd=None,
        investors=[],
        sector=None,
    )
    assert result.is_funding_related is False
    assert result.investors == []


def test_strip_html_collapses_tags_and_whitespace():
    assert _strip_html("<p>Hello   <b>world</b></p>\n\n") == "Hello world"


# --- Dollar-amount parsing -------------------------------------------------

def test_dollar_amounts_various_forms():
    text = "Acme raised $12 million from investors, or $12M as reported, or USD 12,000,000 total."
    amounts = _dollar_amounts_in_text(text)
    assert amounts == [12_000_000.0, 12_000_000.0, 12_000_000.0]


def test_dollar_amounts_handles_billion_and_plain_figure():
    text = "The deal was valued at $1.5 billion, with a $500,000 initial tranche."
    amounts = _dollar_amounts_in_text(text)
    assert amounts == [1_500_000_000.0, 500_000.0]


def test_amount_grounded_true_for_matching_figure():
    assert _amount_grounded(12_000_000.0, "Acme raised $12 million in Series A funding.")


def test_amount_grounded_false_for_absent_figure():
    assert not _amount_grounded(50_000_000.0, "Acme raised $12 million in Series A funding.")


# --- ground_extraction: real grounded cases --------------------------------

def test_ground_extraction_all_fields_grounded():
    text = "Acme Corp raised $12 million in a Series A round led by Sequoia Capital."
    result = ExtractionResult(
        is_funding_related=True,
        company_name="Acme Corp",
        funding_round="series-a",
        amount_usd=12_000_000.0,
        investors=["Sequoia Capital"],
        sector="Fintech",
    )
    grounded = ground_extraction(result, text)
    assert grounded.company_name == "Acme Corp"
    assert grounded.funding_round == "series-a"
    assert grounded.amount_usd == 12_000_000.0
    assert grounded.investors == ["Sequoia Capital"]
    assert grounded.grounding_failures == []


def test_ground_extraction_other_round_exempt_from_keyword_check():
    text = "Acme Corp announced a new funding round."
    result = ExtractionResult(
        is_funding_related=True, company_name="Acme Corp", funding_round="other",
        amount_usd=None, investors=[], sector=None,
    )
    grounded = ground_extraction(result, text)
    assert grounded.funding_round == "other"
    assert grounded.grounding_failures == []


# --- ground_extraction: hallucination cases (the core of Stage 1) ---------

def test_ground_extraction_catches_hallucinated_investor():
    # A deliberate hallucination case: the model claims a second investor
    # that never appears in the source text. That investor must be dropped
    # from the list and logged, while the real investor is kept.
    text = "Acme Corp raised $12 million in a Series A round led by Sequoia Capital."
    result = ExtractionResult(
        is_funding_related=True,
        company_name="Acme Corp",
        funding_round="series-a",
        amount_usd=12_000_000.0,
        investors=["Sequoia Capital", "Fabricated Ventures LLC"],
        sector="Fintech",
    )
    grounded = ground_extraction(result, text)
    assert grounded.investors == ["Sequoia Capital"]
    assert any("Fabricated Ventures LLC" in f for f in grounded.grounding_failures)


def test_ground_extraction_catches_hallucinated_company_name():
    text = "The startup raised $12 million in a Series A round."
    result = ExtractionResult(
        is_funding_related=True, company_name="Totally Fake Inc", funding_round="series-a",
        amount_usd=12_000_000.0, investors=[], sector=None,
    )
    grounded = ground_extraction(result, text)
    assert grounded.company_name is None
    assert any("Totally Fake Inc" in f for f in grounded.grounding_failures)


def test_ground_extraction_catches_hallucinated_amount():
    text = "Acme Corp raised an undisclosed amount in a Series A round."
    result = ExtractionResult(
        is_funding_related=True, company_name="Acme Corp", funding_round="series-a",
        amount_usd=50_000_000.0, investors=[], sector=None,
    )
    grounded = ground_extraction(result, text)
    assert grounded.amount_usd is None
    assert any(f.startswith("amount_usd:") for f in grounded.grounding_failures)


def test_ground_extraction_catches_hallucinated_funding_round():
    text = "Acme Corp raised $12 million in new funding."
    result = ExtractionResult(
        is_funding_related=True, company_name="Acme Corp", funding_round="series-b",
        amount_usd=12_000_000.0, investors=[], sector=None,
    )
    grounded = ground_extraction(result, text)
    assert grounded.funding_round is None
    assert any("series-b" in f for f in grounded.grounding_failures)


def test_ground_extraction_is_funding_related_exempt():
    text = "Nothing about money here."
    result = ExtractionResult(
        is_funding_related=False, company_name=None, funding_round=None,
        amount_usd=None, investors=[], sector=None,
    )
    grounded = ground_extraction(result, text)
    assert grounded.is_funding_related is False
    assert grounded.grounding_failures == []
