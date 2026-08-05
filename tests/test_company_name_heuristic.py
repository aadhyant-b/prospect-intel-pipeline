from src.ingest.company_name_heuristic import (
    MAX_COMPANY_NAME_WORDS,
    _looks_like_company_name,
    guess_company_name,
)


def test_guess_company_name_colon_style():
    assert guess_company_name("Zelluna ASA: Disclosure of large shareholding") == "Zelluna ASA"


def test_guess_company_name_verb_style():
    assert guess_company_name("Acme Corp Announces $10M Series A") == "Acme Corp"


def test_guess_company_name_no_match_returns_none():
    assert guess_company_name("A Report on the State of the Industry") is None


def test_guess_company_name_empty_string():
    assert guess_company_name("") is None


def test_looks_like_company_name_word_count_boundary():
    assert _looks_like_company_name(" ".join(["Word"] * MAX_COMPANY_NAME_WORDS)) is True
    assert _looks_like_company_name(" ".join(["Word"] * (MAX_COMPANY_NAME_WORDS + 1))) is False


def test_guess_company_name_does_not_split_on_partners_inside_a_name():
    assert guess_company_name("Brookfield Business Partners L.P. Announces Third Quarter Call") == (
        "Brookfield Business Partners L.P."
    )


def test_guess_company_name_rejects_wire_notice_prefixes():
    assert guess_company_name("CORRECTION: Ascentage Pharma to Report Results") is None
    assert guess_company_name("Update: The Apache Software Foundation Announces Gradle") is None


def test_guess_company_name_strips_trailing_verb_before_colon():
    assert guess_company_name("Hemp, Inc. Reports: Growing Legalization of Industrial Hemp") == "Hemp, Inc."


def test_guess_company_name_usable_from_a_live_style_title():
    # Same heuristic, applied to a title shape a live RSS feed would produce
    # (src/ingest/pollers.py), not just historical sitemap/slug titles.
    assert guess_company_name("Databahn Raises $40 Million Series B Led by Insight Partners") == "Databahn"
