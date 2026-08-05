from src.extract.company_backfill import CompanyNameResult, _should_skip


def test_should_skip_dotted_token_titles():
    assert _should_skip("Gtm.js") is True
    assert _should_skip("Gtm.start") is True


def test_should_skip_placeholder_title():
    assert _should_skip("(untitled Business Wire release)") is True


def test_should_skip_false_for_real_headline():
    assert _should_skip("Robert Bosch GmbH IAM Express Confidence Tele") is False


def test_company_name_result_accepts_null():
    result = CompanyNameResult(company_name=None)
    assert result.company_name is None


def test_company_name_result_accepts_name():
    result = CompanyNameResult(company_name="Robert Bosch GmbH")
    assert result.company_name == "Robert Bosch GmbH"
