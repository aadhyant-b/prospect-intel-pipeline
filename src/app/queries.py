"""Read-only data access for the funding-leads dashboard (src/app/main.py).

Kept separate from src/db.py (the raw client factory) so the dashboard's
query shape -- and its one Python-side join -- lives in one place. This
module never writes.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime

from src.db import get_client
from src.detect.detector import fetch_releases as _fetch_all_releases
from src.detect.detector import group_releases

LEADS_LIST_LIMIT = 100

# Cadence fallback (see _get_grouped_releases_cached) reuses the switch
# detector's own fetch+group logic rather than duplicating it, which means a
# full releases-table paginated fetch (~10k rows today) on a cache miss --
# fine for a rarely-hit internal dashboard, but cached briefly so repeated
# page loads in quick succession don't refetch the whole table every time.
_CADENCE_CACHE_TTL_SECONDS = 60
_cadence_cache: dict[str, object] = {"data": None, "fetched_at": 0.0}

_TICKER_RE = re.compile(r"[^A-Za-z0-9]")


def _parse_jsonb_list(value) -> list:
    # funding_leads.investors/grounding_failures and switch_predictions.reasons
    # are all written via json.dumps(...) before insert (the convention this
    # codebase already uses for switch_predictions in src/detect/detector.py),
    # so PostgREST returns them as JSON-string scalars, not native arrays --
    # parse before use.
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _format_amount(amount: float | None) -> str:
    if amount is None:
        return "UNDISCLOSED"
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"


def _add_display_fields(lead: dict) -> dict:
    lead["investors"] = _parse_jsonb_list(lead.get("investors"))
    lead["grounding_failures"] = _parse_jsonb_list(lead.get("grounding_failures"))
    lead["amount_usd_fmt"] = _format_amount(lead.get("amount_usd"))
    lead["investor_count"] = len(lead["investors"])
    lead["time_ago"] = _format_relative_time(lead.get("published_at"))
    lead["ticker"] = _synthesize_ticker(lead.get("company_name"), lead.get("id", "----"))
    return lead


def _add_bar_metrics(leads: list[dict]) -> None:
    """Ticker-bar width, log-scaled within the current result set's own
    min/max amount -- a linear scale would flatten every sub-$50M raise next
    to one $1B+ outlier. Leads with no disclosed amount get a flat 0 bar."""
    amounts = [lead["amount_usd"] for lead in leads if lead.get("amount_usd") and lead["amount_usd"] > 0]
    if not amounts:
        for lead in leads:
            lead["bar_pct"] = 0
        return

    log_amounts = [math.log10(a) for a in amounts]
    lo, hi = min(log_amounts), max(log_amounts)
    span = hi - lo

    for lead in leads:
        amount = lead.get("amount_usd")
        if not amount or amount <= 0:
            lead["bar_pct"] = 0
            continue
        if span == 0:
            lead["bar_pct"] = 100
        else:
            pct = (math.log10(amount) - lo) / span * 100
            lead["bar_pct"] = max(8, round(pct))  # floor so small raises still show a sliver


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _format_relative_time(value: str | None) -> str:
    if not value:
        return "—"
    delta = datetime.now(UTC) - _parse_iso(value)
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def _synthesize_ticker(company_name: str | None, fallback_id: str) -> str:
    # Decorative, not a real identifier -- collisions are fine.
    cleaned = _TICKER_RE.sub("", company_name or "").upper()
    return cleaned[:4] if cleaned else fallback_id[:4].upper()


def _get_grouped_releases_cached(client) -> dict[str, list[dict]]:
    now = time.monotonic()
    if _cadence_cache["data"] is not None and (now - _cadence_cache["fetched_at"]) < _CADENCE_CACHE_TTL_SECONDS:
        return _cadence_cache["data"]
    grouped = group_releases(_fetch_all_releases(client))
    _cadence_cache["data"] = grouped
    _cadence_cache["fetched_at"] = now
    return grouped


def _add_activity_field(client, leads: list[dict]) -> None:
    """Per-row activity indicator. A real switch_predictions entry (rare
    today) wins and is shown as the prominent pulsing signal; otherwise fall
    back to a dim, always-present cadence readout ("N releases - last Xd
    ago") pulled from that company's real release history, so the column
    isn't usually empty. group_releases() keys by the same
    company_group_key() funding_leads.company_group_key was written with, so
    the lookup lines up without any extra normalization here."""
    grouped = _get_grouped_releases_cached(client)
    now = datetime.now(UTC)

    for lead in leads:
        signal = lead.get("switch_signal")
        if signal:
            top_reason = signal["reasons"][0] if signal.get("reasons") else f"score {signal['score']:.2f}"
            lead["activity"] = {"kind": "signal", "text": "⚡ ACTIVITY", "detail": top_reason}
            continue

        group = grouped.get(lead.get("company_group_key"))
        if group:
            last_seen = group[-1]["published_at"]
            days_since = (now - last_seen).days
            lead["activity"] = {
                "kind": "cadence",
                "text": f"{len(group)} release{'s' if len(group) != 1 else ''}",
                "detail": f"last {days_since}d ago" if days_since > 0 else "last today",
            }
        else:
            lead["activity"] = {"kind": "none", "text": "—", "detail": ""}


def compute_feed_stats(leads: list[dict]) -> dict:
    total_capital = sum(lead["amount_usd"] for lead in leads if lead.get("amount_usd"))
    return {
        "total_leads": len(leads),
        "total_capital_fmt": _format_amount(total_capital) if total_capital else "$0",
        "verified_count": sum(1 for lead in leads if lead.get("fully_grounded")),
        "last_updated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def _fetch_latest_switch_signals(client, keys: set[str]) -> dict[str, dict]:
    """Loosely-coupled enrichment: PostgREST can't join funding_leads and
    switch_predictions directly (no FK between them, by design), so both are
    fetched and joined in Python by company_group_key -- same in-memory-join
    pattern src/extract/leads_ingester.py already uses for its idempotency
    check. Leads display fine when nothing comes back here."""
    if not keys:
        return {}
    rows = (
        client.table("switch_predictions")
        .select("company_group_key,score,reasons,detected_at")
        .in_("company_group_key", list(keys))
        .order("detected_at", desc=True)
        .execute()
        .data
    )
    latest: dict[str, dict] = {}
    for row in rows:
        key = row["company_group_key"]
        if key not in latest:  # rows are already ordered desc -- first hit wins
            row["reasons"] = _parse_jsonb_list(row.get("reasons"))
            latest[key] = row
    return latest


def fetch_leads(limit: int = LEADS_LIST_LIMIT) -> list[dict]:
    client = get_client()
    leads = (
        client.table("funding_leads")
        .select("*")
        .order("published_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )
    for lead in leads:
        _add_display_fields(lead)
    _add_bar_metrics(leads)

    keys = {lead["company_group_key"] for lead in leads if lead.get("company_group_key")}
    signals = _fetch_latest_switch_signals(client, keys)
    for lead in leads:
        lead["switch_signal"] = signals.get(lead.get("company_group_key"))

    _add_activity_field(client, leads)

    return leads


def fetch_lead_detail(lead_id: str) -> dict | None:
    client = get_client()
    rows = client.table("funding_leads").select("*").eq("id", lead_id).limit(1).execute().data
    if not rows:
        return None
    lead = _add_display_fields(rows[0])

    release = None
    if lead.get("release_id"):
        release_rows = (
            client.table("releases")
            .select("id,title,url,distributor,published_at")
            .eq("id", lead["release_id"])
            .limit(1)
            .execute()
            .data
        )
        release = release_rows[0] if release_rows else None
    lead["release"] = release

    signals = _fetch_latest_switch_signals(client, {lead["company_group_key"]} if lead.get("company_group_key") else set())
    lead["switch_signal"] = signals.get(lead.get("company_group_key"))

    _add_activity_field(client, [lead])

    return lead
