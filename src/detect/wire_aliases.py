"""Distributor alias map (Track C1).

Normalizes raw `releases.distributor` values to a canonical wire identity,
applied BEFORE any wire-change comparison in src/detect/detector.py -- so a
wire rebrand, acquisition, or (as below) our own poller subscribing to two
RSS feeds off the same underlying wire never reads as a company switching
distributors.

"PR Newswire" / "PR Newswire - Venture Capital" is not a hypothetical: it's
our own src/ingest/pollers.py::FEEDS polling the same wire's general feed and
its venture-capital-topic feed separately. Without this map, any company
whose funding round shows up on both (very likely, by design of the VC feed)
would falsely register as a wire-change on day one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WIRE_ALIASES: dict[str, str] = {
    "pr newswire": "PR Newswire",
    "pr newswire - venture capital": "PR Newswire",
    "globenewswire": "GlobeNewswire",
    "business wire": "Business Wire",
    # Motivating example for a real rebrand/acquisition entry, not yet a
    # live/historical source in our data: ACCESS Newswire acquired
    # Newswire.com -- if we ever ingest either, both map to one canonical name:
    # "newswire.com": "ACCESS Newswire",
    # "access newswire": "ACCESS Newswire",
}


def canonical_distributor(raw: str) -> str:
    key = raw.strip().lower()
    canonical = WIRE_ALIASES.get(key)
    if canonical is None:
        logger.warning("unmapped_distributor distributor=%r", raw)
        return raw.strip()
    return canonical
