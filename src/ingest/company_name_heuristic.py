"""Best-effort company-name extraction from a press-release title.

Shared by src/ingest/historical.py (titles from GlobeNewswire's sitemap and
Wayback-derived slugs) and src/ingest/pollers.py (titles from live RSS
feeds) -- both need the same cheap, ungated grouping signal so the switch
detector sees one consistent company_name_raw across the whole `releases`
timeline, live or historical, regardless of whether extraction has ever run.

Deliberately conservative: MAX_COMPANY_NAME_WORDS and the denylist below
exist because a wrong guess actively corrupts the switch detector's
per-company grouping, which is worse than leaving a gap for
src/extract/company_backfill.py (or Track D) to fill in later.
"""

from __future__ import annotations

import re

# "partners" is deliberately excluded even though "X Partners With Y" is a
# real headline pattern: it's also an extremely common word *inside* real
# company names (VC/PE naming convention -- "Acme Capital Partners", "Acme
# Business Partners L.P."), and the false-mid-name-split it causes there
# ("Brookfield Business Partners L.P." -> "Brookfield Business") is a worse
# error than losing the "X Partners With Y" match.
_ANNOUNCE_VERBS = [
    "announces", "announced", "reports", "reported", "completes", "completed",
    "closes", "closed", "raises", "raised", "secures", "secured", "appoints",
    "appointed", "names", "named", "launches", "launched", "unveils", "unveiled",
    "receives", "received", "provides", "signs", "signed", "enters", "entered",
    "acquires", "acquired", "expands", "expanded", "discloses",
    "disclosed", "files", "filed", "opens", "opened", "wins", "won",
    "introduces", "introduced", "extends", "extended", "publishes", "published",
    "releases", "released", "achieves", "achieved", "celebrates", "celebrated",
]
_VERB_SPLIT_RE = re.compile(
    r"^(.{2,80}?)\s+(?:" + "|".join(_ANNOUNCE_VERBS) + r")\b", re.IGNORECASE
)
_COLON_SPLIT_RE = re.compile(r"^([A-Za-z0-9&.,'\-\s]{2,60}?):\s+\S")

# A real company name is almost always short. Both the colon- and verb-split
# regexes above are otherwise unbounded in *shape* -- they'll happily capture
# an entire descriptive clause ("Minuteman Press Franchise Owner Ken
# Holloway") or a marketing tagline that precedes the real subject rather
# than naming it ("The Soccer League That Never Stops: LegaBot, the World's
# First..."). A word-count gate rejects those rather than returning a wrong
# guess -- None is the honest answer when a title's structure doesn't put a
# company name in a position this heuristic can find.
MAX_COMPANY_NAME_WORDS = 5

# Wire notice-prefixes: a small, closed, recurring set of editorial labels
# that precede an unrelated headline (e.g. "CORRECTION: <headline>",
# "Update: <headline>") -- short enough to pass the word-count gate but never
# a company name. Unlike person-name or generic-phrase false positives (no
# fixable shared pattern), this class is exact-match denylistable.
_NON_COMPANY_PREFIXES = {
    "correction", "update", "repeat", "clarification", "advisory",
    "bulletin", "flash", "flash news", "editor's note", "correction repeat",
}


def _looks_like_company_name(candidate: str) -> bool:
    words = candidate.split()
    if not (1 <= len(words) <= MAX_COMPANY_NAME_WORDS):
        return False
    if ":" in candidate:
        return False
    return candidate.strip().lower() not in _NON_COMPANY_PREFIXES


def _strip_trailing_verb(candidate: str) -> str:
    # The verb-split regex captures everything BEFORE the verb by
    # construction, but the colon-split regex doesn't -- "Company Reports:
    # <headline>" is a real, common wire convention, and without this the
    # colon path wrongly includes "Reports" in the name.
    words = candidate.split()
    if words and words[-1].lower().rstrip(".,") in _ANNOUNCE_VERBS:
        words = words[:-1]
    return " ".join(words)


def guess_company_name(text: str) -> str | None:
    if not text:
        return None
    colon_match = _COLON_SPLIT_RE.match(text)
    if colon_match:
        candidate = _strip_trailing_verb(colon_match.group(1).strip())
        if _looks_like_company_name(candidate):
            return candidate
    verb_match = _VERB_SPLIT_RE.match(text)
    if verb_match:
        candidate = verb_match.group(1).strip()
        if _looks_like_company_name(candidate):
            return candidate
    return None
