from src.maintenance.dedupe_funding_leads import find_duplicate_groups


def _row(id, company_group_key, amount_usd, published_at, created_at, funding_round="series-b"):
    return {
        "id": id,
        "company_group_key": company_group_key,
        "company_name": company_group_key,
        "funding_round": funding_round,
        "amount_usd": amount_usd,
        "published_at": published_at,
        "created_at": created_at,
    }


def test_no_duplicates_returns_empty():
    rows = [
        _row("a", "acme", 1_000_000.0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        _row("b", "beta", 1_000_000.0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ]
    assert find_duplicate_groups(rows) == []


def test_same_round_reported_by_several_releases_is_a_duplicate():
    rows = [
        _row("a", "vaderis", 152_000_000.0, "2026-08-11T11:54:00+00:00", "2026-08-20T07:44:50Z"),
        _row("b", "vaderis", 152_000_000.0, "2026-08-11T11:00:00+00:00", "2026-08-20T07:44:53Z"),
        _row("c", "vaderis", 152_000_000.0, "2026-08-11T11:00:00+00:00", "2026-08-20T07:44:57Z"),
    ]
    groups = find_duplicate_groups(rows)
    assert len(groups) == 1
    assert {r["id"] for r in groups[0]} == {"a", "b", "c"}


def test_translated_rerelease_on_next_utc_day_is_a_duplicate():
    rows = [
        _row("a", "vaderis", 152_000_000.0, "2026-08-11T23:30:00Z", "2026-08-20T07:44:50Z"),
        _row("b", "vaderis", 151_800_000.0, "2026-08-12T06:00:00Z", "2026-08-20T07:44:53Z"),
    ]
    groups = find_duplicate_groups(rows)
    assert len(groups) == 1


def test_different_amount_is_not_a_duplicate():
    rows = [
        _row("a", "acme", 152_000_000.0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        _row("b", "acme", 148_000_000.0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ]
    assert find_duplicate_groups(rows) == []


def test_weak_rounds_group_within_a_month_only():
    rows = [
        _row("a", "nasn", None, "2026-08-07T10:00:00Z", "2026-08-09T19:29:14Z", funding_round="other"),
        _row("b", "nasn", None, "2026-08-07T12:00:00Z", "2026-08-09T19:29:18Z", funding_round="other"),
        _row("c", "nasn", None, "2026-09-07T12:00:00Z", "2026-09-09T19:29:18Z", funding_round="other"),
    ]
    groups = find_duplicate_groups(rows)
    assert len(groups) == 1
    assert {r["id"] for r in groups[0]} == {"a", "b"}
