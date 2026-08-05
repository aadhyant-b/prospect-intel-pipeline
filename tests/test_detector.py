from datetime import UTC, datetime, timedelta

import pytest
from src.detect.detector import group_releases, run_detector, score_company

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _rows(specs, distributor="PR Newswire", company="Acme Corp"):
    """specs: list of day offsets from BASE."""
    return [
        {
            "id": f"r{i}",
            "published_at": (BASE + timedelta(days=d)).isoformat(),
            "distributor": distributor,
            "company_name_raw": company,
        }
        for i, d in enumerate(specs)
    ]


def _releases_for_score(specs, distributor="PR Newswire"):
    return [
        {"id": f"r{i}", "published_at": BASE + timedelta(days=d), "distributor": distributor}
        for i, d in enumerate(specs)
    ]


# --- group_releases ----------------------------------------------------------


def test_group_releases_merges_suffix_variants():
    rows = _rows([0, 10], company="Acme Inc.") + _rows([20], company="Acme Corp")
    groups = group_releases(rows)
    assert set(groups) == {"acme"}
    assert len(groups["acme"]) == 3


def test_group_releases_drops_rows_with_no_company_signal():
    rows = _rows([0], company=None) + _rows([10], company="")
    groups = group_releases(rows)
    assert groups == {}


# --- gone-quiet (isolated: constant 30-day cadence, gone silent) -------------


def test_gone_quiet_score_exact():
    # 3 releases every 30 days, then silent until as_of = day 160
    # (100 days since the last release). baseline_gap = 30.
    releases = _releases_for_score([0, 30, 60])
    as_of = BASE + timedelta(days=160)

    result = score_company("acme", releases, as_of)

    assert result is not None
    assert result.baseline_gap_days == pytest.approx(30.0)
    # ratio = 100/30 = 3.3333..., gone_quiet_score = min(ratio/3.0, 2.0) = 10/9
    assert result.signals["gone_quiet_score"] == pytest.approx(10 / 9, rel=1e-6)
    # Equal gaps -> recency-weighted average equals the plain average -> ratio 1 -> no drop.
    assert result.signals["cadence_drop_score"] == pytest.approx(0.0)
    assert result.signals["wire_change_score"] == pytest.approx(0.0)
    assert any("Gone quiet" in r for r in result.reasons)


# --- cadence-drop (isolated: recent gap wider than baseline, as_of = last release) --


def test_cadence_drop_score_exact():
    # Gaps of 30, 30, 30, then a recent gap of 60 (day 90 -> 150).
    # baseline_gap = median([30, 30, 30, 60]) = 30.
    # as_of = 150 = last release -> gone_quiet_score = 0 (isolates cadence-drop).
    releases = _releases_for_score([0, 30, 60, 90, 150])
    as_of = BASE + timedelta(days=150)

    result = score_company("acme", releases, as_of)

    assert result is not None
    assert result.signals["gone_quiet_score"] == pytest.approx(0.0)
    # Hand-derived (see PR description / conversation): weights at ages
    # 120,90,60,0 days with a 30-day half-life are exact powers of 0.5
    # (1/16, 1/8, 1/4, 1). weighted_gap = 1170/23 -> ratio = 1170/690.
    # cadence_drop_score = (ratio - 1) = 480/690 = 16/23.
    assert result.signals["cadence_drop_score"] == pytest.approx(16 / 23, rel=1e-6)
    assert any("Cadence slowing" in r for r in result.reasons)


# --- wire-change (isolated: constant cadence, last releases on a new wire) --


def test_wire_change_score_exact():
    # 6 releases every 20 days -> baseline_gap = 20, equal gaps -> no
    # gone-quiet/cadence-drop signal (as_of = last release).
    # First 4 on PR Newswire, last 2 on GlobeNewswire.
    specs = [0, 20, 40, 60, 80, 100]
    releases = [
        {"id": f"r{i}", "published_at": BASE + timedelta(days=d),
         "distributor": "PR Newswire" if i < 4 else "GlobeNewswire"}
        for i, d in enumerate(specs)
    ]
    as_of = BASE + timedelta(days=100)

    result = score_company("acme", releases, as_of)

    assert result is not None
    assert result.signals["gone_quiet_score"] == pytest.approx(0.0)
    assert result.signals["cadence_drop_score"] == pytest.approx(0.0)
    # recent_k = min(3, 5) = 3 -> recent = [PRN(idx3), GNW(idx4), GNW(idx5)]
    # historical = releases[:3] = all PRN -> new_distributors = {GlobeNewswire}
    # new_count = 2 (the two GNW entries in the recent window) -> 2/3.
    assert result.signals["wire_change_score"] == pytest.approx(2 / 3, rel=1e-6)
    assert any("Wire change" in r for r in result.reasons)


def test_wire_change_respects_alias_map():
    # PR Newswire and PR Newswire - Venture Capital must NOT register as a
    # wire change -- this is the real, non-hypothetical case from our own
    # poller (src/ingest/pollers.py::FEEDS).
    rows = (
        _rows([0, 10, 20, 30], distributor="PR Newswire")
        + _rows([40, 50], distributor="PR Newswire - Venture Capital")
    )
    groups = group_releases(rows)
    releases = groups["acme"]  # normalize_company_name("Acme Corp") == "acme"
    as_of = BASE + timedelta(days=50)

    result = score_company("acme", releases, as_of)

    assert result is not None
    assert result.signals["wire_change_score"] == pytest.approx(0.0)


# --- eligibility / volume weight ---------------------------------------------


def test_below_minimum_releases_returns_none():
    releases = _releases_for_score([0, 30])  # only 2 releases, 1 gap
    as_of = BASE + timedelta(days=200)
    assert score_company("acme", releases, as_of) is None


def test_volume_weight_scales_with_release_count():
    releases_thin = _releases_for_score([0, 30, 60])  # n=3
    releases_rich = _releases_for_score(list(range(0, 330, 30)))  # n=11
    as_of = BASE + timedelta(days=500)

    thin = score_company("acme", releases_thin, as_of)
    rich = score_company("acme", releases_rich, as_of)

    assert thin.signals["volume_weight"] == pytest.approx(0.3)
    assert rich.signals["volume_weight"] == pytest.approx(1.0)


# --- run_detector: as_of filtering (the backtest-enabling behavior) ---------


def test_run_detector_as_of_hides_future_releases():
    # 4 releases on PR Newswire (eligible baseline), then 2 more AFTER the
    # cutoff on a brand-new wire. A run "as of" the cutoff must not see the
    # future wire-change; a run at the true present must.
    past_rows = _rows([0, 20, 40, 60], distributor="PR Newswire")
    future_rows = _rows([80, 100], distributor="GlobeNewswire")
    for i, row in enumerate(future_rows):
        row["id"] = f"future{i}"
    all_rows = past_rows + future_rows

    cutoff = BASE + timedelta(days=60)
    flagged_at_cutoff = run_detector(all_rows, as_of=cutoff, flag_threshold=-1.0)
    flagged_now = run_detector(all_rows, as_of=BASE + timedelta(days=100), flag_threshold=-1.0)

    assert flagged_at_cutoff[0].signals["wire_change_score"] == pytest.approx(0.0)
    assert flagged_now[0].signals["wire_change_score"] > 0.0


def test_run_detector_sorts_by_score_descending():
    quiet_company = _rows([0, 30, 60], company="Quiet Co")
    active_company = _rows([0, 10, 20], company="Active Co")
    as_of = BASE + timedelta(days=200)

    flagged = run_detector(quiet_company + active_company, as_of=as_of, flag_threshold=-1.0)

    assert len(flagged) == 2
    assert flagged[0].score >= flagged[1].score
