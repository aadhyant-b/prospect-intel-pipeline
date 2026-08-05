"""Company-identity normalization -- a stand-in for full entity resolution.

Track D (entity resolution) hasn't been built yet, so the switch detector
(src/detect/detector.py) needs *some* way to group releases by company today.
This module provides that: exact-normalized-key grouping (legal-suffix
stripping + lowercasing -- the same rule set already shipped and tested in
src/eval/evaluator.py for gold-set scoring), not full resolution.

Explicitly out of scope here, deferred to Track D: fuzzy near-duplicate
merging across *distinct* normalized keys (e.g. "Acme Cyber Security" vs
"Acme CyberSecurity"), embedding-based resolution for rebrands/DBAs, and
writing anything to the `companies` table. This module never touches the
database -- it's a pure string function the detector calls in memory.
"""

from __future__ import annotations

import re

# Longest-first so e.g. "Corporation" is stripped before a shorter overlapping
# suffix could partially match it. Identical rule set to
# src/eval/evaluator.py::_LEGAL_SUFFIXES -- kept as a separate copy rather
# than a cross-module import since evaluator.py is scoped to eval/scoring and
# shouldn't gain a dependency from src.detect.
_LEGAL_SUFFIXES = [
    "corporation", "incorporated", "company",
    "l.l.c.", "l.l.c", "llc", "plc",
    "corp.", "corp", "inc.", "inc",
    "ltd.", "ltd", "co.", "co",
]
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\.?$",
    re.IGNORECASE,
)


def normalize_company_name(name: str) -> str:
    normalized = name.strip().lower()
    while True:
        stripped = _LEGAL_SUFFIX_RE.sub("", normalized).strip()
        if stripped == normalized:
            return normalized
        normalized = stripped


def company_group_key(name: str | None) -> str | None:
    """Grouping key for a release's company_name_raw, or None if the release
    carries no usable company signal (name is None, or '' -- the
    src/extract/company_backfill.py sentinel for "confirmed no company").
    """
    if not name:
        return None
    normalized = normalize_company_name(name)
    return normalized or None
