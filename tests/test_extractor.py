from src.extract.extractor import ExtractionResult, _strip_html


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
