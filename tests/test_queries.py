from src.app.queries import _add_bar_metrics, _format_amount, _parse_jsonb_list


def test_parse_jsonb_list_decodes_json_string():
    assert _parse_jsonb_list('["A Ventures", "B Capital"]') == ["A Ventures", "B Capital"]


def test_parse_jsonb_list_handles_none():
    assert _parse_jsonb_list(None) == []


def test_parse_jsonb_list_handles_already_a_list():
    # Defensive: if PostgREST ever returns a native array, don't double-parse.
    assert _parse_jsonb_list(["A Ventures"]) == ["A Ventures"]


def test_parse_jsonb_list_handles_malformed_json():
    assert _parse_jsonb_list("not json") == []


def test_format_amount_tiers():
    assert _format_amount(None) == "UNDISCLOSED"
    assert _format_amount(500) == "$500"
    assert _format_amount(50_000) == "$50K"
    assert _format_amount(6_500_000) == "$6.5M"
    assert _format_amount(1_500_000_000) == "$1.50B"


def test_add_bar_metrics_scales_log_within_result_set():
    leads = [
        {"amount_usd": 1_000_000},
        {"amount_usd": 1_000_000_000},
        {"amount_usd": None},
    ]
    _add_bar_metrics(leads)

    assert leads[0]["bar_pct"] == 8  # min value floors to the 8% minimum sliver
    assert leads[1]["bar_pct"] == 100  # max value in the set
    assert leads[2]["bar_pct"] == 0  # no disclosed amount -> flat bar


def test_add_bar_metrics_handles_all_equal_amounts():
    leads = [{"amount_usd": 5_000_000}, {"amount_usd": 5_000_000}]
    _add_bar_metrics(leads)
    assert leads[0]["bar_pct"] == 100
    assert leads[1]["bar_pct"] == 100


def test_add_bar_metrics_handles_empty_list():
    leads = []
    _add_bar_metrics(leads)  # must not raise
    assert leads == []
