from datetime import UTC, datetime, timedelta

from src.app import queries
from src.app.queries import (
    _add_activity_field,
    _add_bar_metrics,
    _format_amount,
    _format_relative_time,
    _parse_jsonb_list,
    _synthesize_ticker,
    compute_feed_stats,
)


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


# --- Relative time formatting -----------------------------------------

def test_format_relative_time_handles_none():
    assert _format_relative_time(None) == "—"


def test_format_relative_time_tiers():
    now = datetime.now(UTC)
    assert _format_relative_time((now - timedelta(seconds=10)).isoformat()) == "just now"
    assert _format_relative_time((now - timedelta(minutes=30)).isoformat()) == "30m ago"
    assert _format_relative_time((now - timedelta(hours=5)).isoformat()) == "5h ago"
    assert _format_relative_time((now - timedelta(days=3)).isoformat()) == "3d ago"


# --- Ticker synthesis ---------------------------------------------------

def test_synthesize_ticker_strips_punctuation_and_truncates():
    assert _synthesize_ticker("Acme, Inc.", "fallback-id") == "ACME"
    assert _synthesize_ticker("A.B. Robotics", "fallback-id") == "ABRO"


def test_synthesize_ticker_falls_back_to_id_when_no_name():
    assert _synthesize_ticker(None, "abcd1234") == "ABCD"
    assert _synthesize_ticker("", "abcd1234") == "ABCD"


# --- Feed stats -----------------------------------------------------------

def test_compute_feed_stats_aggregates_totals():
    leads = [
        {"amount_usd": 10_000_000.0, "fully_grounded": True},
        {"amount_usd": 5_000_000.0, "fully_grounded": False},
        {"amount_usd": None, "fully_grounded": True},
    ]
    stats = compute_feed_stats(leads)
    assert stats["total_leads"] == 3
    assert stats["total_capital_fmt"] == "$15.0M"
    assert stats["verified_count"] == 2
    assert "UTC" in stats["last_updated"]


def test_compute_feed_stats_handles_empty_list():
    stats = compute_feed_stats([])
    assert stats["total_leads"] == 0
    assert stats["total_capital_fmt"] == "$0"
    assert stats["verified_count"] == 0


# --- Activity field: switch signal vs cadence fallback vs none -----------

def test_add_activity_field_prefers_switch_signal_over_cadence(monkeypatch):
    monkeypatch.setattr(
        queries, "_get_grouped_releases_cached",
        lambda client: {"acme": [{"published_at": datetime.now(UTC)}] * 3},
    )
    leads = [{
        "company_group_key": "acme",
        "switch_signal": {"score": 0.8, "reasons": ["Gone quiet: 90 days"]},
    }]
    _add_activity_field(client=None, leads=leads)

    assert leads[0]["activity"]["kind"] == "signal"
    assert leads[0]["activity"]["detail"] == "Gone quiet: 90 days"


def test_add_activity_field_falls_back_to_cadence_when_no_signal(monkeypatch):
    last_seen = datetime.now(UTC) - timedelta(days=5)
    monkeypatch.setattr(
        queries, "_get_grouped_releases_cached",
        lambda client: {"acme": [{"published_at": last_seen}] * 3},
    )
    leads = [{"company_group_key": "acme", "switch_signal": None}]
    _add_activity_field(client=None, leads=leads)

    assert leads[0]["activity"]["kind"] == "cadence"
    assert leads[0]["activity"]["text"] == "3 releases"
    assert "5d ago" in leads[0]["activity"]["detail"]


def test_add_activity_field_none_when_no_data_at_all(monkeypatch):
    monkeypatch.setattr(queries, "_get_grouped_releases_cached", lambda client: {})
    leads = [{"company_group_key": "unknown-co", "switch_signal": None}]
    _add_activity_field(client=None, leads=leads)

    assert leads[0]["activity"]["kind"] == "none"
