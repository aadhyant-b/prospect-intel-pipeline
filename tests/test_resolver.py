from src.resolve.resolver import company_group_key, normalize_company_name


def test_normalize_strips_legal_suffix_and_lowercases():
    assert normalize_company_name("Acme Corp.") == "acme"
    assert normalize_company_name("Acme Corporation") == "acme"
    assert normalize_company_name("Acme Inc.") == "acme"


def test_normalize_collapses_near_duplicates_to_same_key():
    assert normalize_company_name("Acme Inc.") == normalize_company_name("Acme Corp")


def test_company_group_key_none_for_missing_or_empty():
    assert company_group_key(None) is None
    assert company_group_key("") is None


def test_company_group_key_returns_normalized_string():
    assert company_group_key("Acme Corp.") == "acme"
