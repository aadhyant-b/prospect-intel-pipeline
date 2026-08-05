from src.detect.wire_aliases import canonical_distributor


def test_pr_newswire_venture_capital_merges_into_pr_newswire():
    # Real, non-hypothetical case: our own poller subscribes to both feeds
    # off the same underlying wire (src/ingest/pollers.py::FEEDS).
    assert canonical_distributor("PR Newswire - Venture Capital") == "PR Newswire"
    assert canonical_distributor("PR Newswire") == "PR Newswire"


def test_case_insensitive():
    assert canonical_distributor("globenewswire") == "GlobeNewswire"
    assert canonical_distributor("GLOBENEWSWIRE") == "GlobeNewswire"


def test_unknown_distributor_passes_through_unchanged():
    assert canonical_distributor("Some New Wire") == "Some New Wire"
