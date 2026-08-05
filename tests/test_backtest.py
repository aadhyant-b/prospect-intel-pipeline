from datetime import UTC, datetime, timedelta

from src.detect.backtest import run_backtest

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _rows(specs, distributor="PR Newswire", company="Acme Corp", id_prefix="r"):
    return [
        {
            "id": f"{id_prefix}{i}",
            "published_at": (BASE + timedelta(days=d)).isoformat(),
            "distributor": distributor,
            "company_name_raw": company,
        }
        for i, d in enumerate(specs)
    ]


def test_confirmed_switch_true_positive():
    # Flagged at cutoff (wire-change signal from cross-posting isn't present
    # here -- flagged purely on gone-quiet, since PR Newswire history stops
    # at day 60 and cutoff is day 160), and a post-cutoff release really
    # does land on a new wire -- should be a true positive.
    past = _rows([0, 30, 60], distributor="PR Newswire", company="Quiet Co")
    future = _rows([200], distributor="GlobeNewswire", company="Quiet Co", id_prefix="f")
    rows = past + future
    cutoff = BASE + timedelta(days=160)

    result = run_backtest(rows, cutoff, flag_threshold=-1.0)

    assert result.outcomes["quiet"] == "confirmed_switch"
    assert result.tp == 1
    assert result.fp == 0


def test_no_switch_observed_false_positive():
    # Flagged (gone quiet), but post-cutoff releases stay on the same wire --
    # a false positive.
    past = _rows([0, 30, 60], distributor="PR Newswire", company="Quiet Co")
    future = _rows([200, 220], distributor="PR Newswire", company="Quiet Co", id_prefix="f")
    rows = past + future
    cutoff = BASE + timedelta(days=160)

    result = run_backtest(rows, cutoff, flag_threshold=-1.0)

    assert result.outcomes["quiet"] == "no_switch_observed"
    assert result.fp == 1
    assert result.tp == 0


def test_unknown_excluded_from_confusion_matrix():
    # No post-cutoff releases at all -- we genuinely don't know the outcome.
    rows = _rows([0, 30, 60], distributor="PR Newswire", company="Quiet Co")
    cutoff = BASE + timedelta(days=160)

    result = run_backtest(rows, cutoff, flag_threshold=-1.0)

    assert result.outcomes["quiet"] == "unknown"
    assert result.tp == result.fp == result.fn == result.tn == 0


def test_validation_days_window_excludes_late_switch():
    # A switch does eventually happen, but outside the validation window --
    # should read as "unknown" (no data inside the window), not a miss.
    past = _rows([0, 30, 60], distributor="PR Newswire", company="Quiet Co")
    late_future = _rows([500], distributor="GlobeNewswire", company="Quiet Co", id_prefix="f")
    rows = past + late_future
    cutoff = BASE + timedelta(days=160)

    result = run_backtest(rows, cutoff, validation_days=30, flag_threshold=-1.0)

    assert result.outcomes["quiet"] == "unknown"


def test_as_of_cutoff_prevents_leakage_into_flagging():
    # Company looks completely healthy up to the cutoff (steady cadence, no
    # gone-quiet/cadence-drop/wire-change signal) -- it must not be flagged,
    # even though it happens to switch wires later. Confirms run_backtest
    # relies on run_detector's as_of filtering rather than peeking ahead.
    past = _rows([0, 20, 40, 60], distributor="PR Newswire", company="Healthy Co")
    future = _rows([70, 90], distributor="GlobeNewswire", company="Healthy Co", id_prefix="f")
    rows = past + future
    cutoff = BASE + timedelta(days=60)

    result = run_backtest(rows, cutoff, flag_threshold=0.5)

    assert "healthy" not in {f.group_key for f in result.flagged}
    # But the ground truth still correctly shows the switch happened (this
    # would be a false negative if a threshold were low enough to flag it).
    assert result.outcomes["healthy"] == "confirmed_switch"
