"""Read-only data access for the funding-leads dashboard (src/app/main.py).

Kept separate from src/db.py (the raw client factory) so the dashboard's
query shape -- and its one Python-side join -- lives in one place. This
module never writes.
"""

from __future__ import annotations

import json
import math

from src.db import get_client

LEADS_LIST_LIMIT = 100


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

    return lead
