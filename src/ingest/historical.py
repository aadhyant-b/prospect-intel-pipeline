"""Historical release-metadata backfill (Track B1).

Pulls (company, date, distributor, url) metadata -- not full text -- from
two sources the live RSS pollers can't reach:

- GlobeNewswire's own sitemap (structured, back to ~2023-09).
- Wayback Machine CDX index of businesswire.com release permalinks
  (metadata only -- URL, capture timestamp -- back to ~2003; Business Wire
  has no public live feed, see src/ingest/pollers.py).

Writes into the same `releases` table the live pollers use (see
migrations/006_release_provenance.sql), tagged via `source` so the switch
detector can account for per-source coverage completeness, and with a
best-effort `company_name_raw` since these rows have no raw_text to run
through the extraction pipeline for a real company_name.

Run via `python -m src.ingest.historical [--source ...] [--limit N]`.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
from pydantic import BaseModel

from src.db import get_client
from src.ingest.company_name_heuristic import guess_company_name as _guess_company_name
from src.ingest.pollers import USER_AGENT

logger = logging.getLogger(__name__)

GLOBENEWSWIRE_SITEMAP_INDEX = "https://sitemaps.globenewswire.com/news-en.xml"
WAYBACK_CDX_URL = "http://web.archive.org/cdx/search/cdx"

REQUEST_TIMEOUT_SECONDS = 30
GNW_DELAY_SECONDS = 1.0
WAYBACK_DELAY_SECONDS = 1.0
WAYBACK_PAGE_SIZE = 5000

DEFAULT_LIMIT = 50
# A single GlobeNewswire month or a single early Wayback year each contain far
# more than any reasonable --limit, so an unbounded walk exhausts the whole
# budget within one period -- every company's "3 releases" then lands within
# days of each other (a burst), not spread over real time, which makes
# baseline_gap_days collapse to ~0 and the switch detector's cadence math
# meaningless. Capping how much any single period can contribute forces the
# walk to keep moving across periods instead, so releases -- and therefore a
# given company's releases -- end up genuinely spread across months/years.
DEFAULT_PER_PERIOD_LIMIT = 100
WAYBACK_START_YEAR = 2002

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}

_BW_URL_RE = re.compile(r"/news/home/(?P<date>\d{8})\d*/en/?(?P<slug>[^/?]*)", re.IGNORECASE)


class HistoricalRelease(BaseModel):
    url: str
    title: str
    distributor: str
    published_at: datetime
    source: str
    company_name_raw: str | None


def _slug_to_title(slug: str) -> str:
    words = slug.replace("-", " ").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else words


# --- GlobeNewswire sitemap -------------------------------------------------


def _fetch_globenewswire_sitemap_urls(http_client: httpx.Client) -> list[str]:
    response = http_client.get(GLOBENEWSWIRE_SITEMAP_INDEX, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    return [
        loc.text
        for loc in root.findall("sm:sitemap/sm:loc", SITEMAP_NS)
        if loc.text and "latest.xml" not in loc.text
    ]


def _parse_globenewswire_entry(url_elem: ET.Element) -> HistoricalRelease | None:
    loc = url_elem.findtext("sm:loc", namespaces=SITEMAP_NS)
    title = url_elem.findtext("news:news/news:title", namespaces=SITEMAP_NS)
    pub_date = url_elem.findtext("news:news/news:publication_date", namespaces=SITEMAP_NS)
    if not (loc and title and pub_date):
        return None
    try:
        published_at = datetime.fromisoformat(pub_date)
    except ValueError:
        return None
    return HistoricalRelease(
        url=loc,
        title=title,
        distributor="GlobeNewswire",
        published_at=published_at,
        source="globenewswire-sitemap",
        company_name_raw=_guess_company_name(title),
    )


def fetch_globenewswire(
    limit: int,
    http_client: httpx.Client,
    month: str | None = None,
    per_period_limit: int | None = DEFAULT_PER_PERIOD_LIMIT,
) -> tuple[list[HistoricalRelease], int]:
    """per_period_limit caps how many releases any single month can
    contribute (None = unlimited, i.e. the old exhaust-one-month behavior).
    Default is capped so the walk spreads across all available months."""
    releases: list[HistoricalRelease] = []
    seen = 0

    if month:
        # Targeted single-month fetch, for deterministic historical spot-checks
        # -- bypasses the index entirely and constructs the URL directly.
        monthly_urls = [f"https://sitemaps.globenewswire.com/news/en/{month}.xml"]
    else:
        try:
            monthly_urls = _fetch_globenewswire_sitemap_urls(http_client)
        except (httpx.HTTPError, ET.ParseError) as exc:
            logger.error("globenewswire_index_failed error=%s", exc)
            return releases, seen
        # The index lists newest month first. Walking oldest-first means a
        # small --limit run actually demonstrates historical reach (the whole
        # point of this ingester) instead of exhausting the limit within the
        # current month alone, which has far more than any small --limit on
        # its own -- that was the bug: the loop below breaks as soon as
        # `limit` is hit, and with the newest-first order it never got past
        # month #1. Oldest-first also means an interrupted full backfill has
        # already captured the exclusive historical data (the live pollers
        # don't have it) before spending time on recent months they do.
        monthly_urls = list(reversed(monthly_urls))

    for monthly_url in monthly_urls:
        if len(releases) >= limit:
            break
        try:
            response = http_client.get(monthly_url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError) as exc:
            logger.warning("globenewswire_month_failed url=%s error=%s", monthly_url, exc)
            time.sleep(GNW_DELAY_SECONDS)
            continue

        month_budget = limit - len(releases)
        if per_period_limit is not None:
            month_budget = min(month_budget, per_period_limit)
        month_taken = 0

        for url_elem in root.findall("sm:url", SITEMAP_NS):
            if len(releases) >= limit or month_taken >= month_budget:
                break
            seen += 1
            release = _parse_globenewswire_entry(url_elem)
            if release is not None:
                releases.append(release)
                month_taken += 1

        time.sleep(GNW_DELAY_SECONDS)

    return releases, seen


# --- Wayback CDX / businesswire.com ----------------------------------------


def _parse_wayback_row(original_url: str) -> HistoricalRelease | None:
    match = _BW_URL_RE.search(original_url)
    if not match:
        return None
    try:
        published_at = datetime.strptime(match.group("date"), "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None

    slug = unquote(match.group("slug") or "").strip("/")
    title = _slug_to_title(slug) if slug else "(untitled Business Wire release)"

    return HistoricalRelease(
        url=original_url,
        title=title,
        distributor="Business Wire",
        published_at=published_at,
        source="wayback-businesswire",
        company_name_raw=_guess_company_name(title),
    )


def _fetch_wayback_range(
    limit: int, http_client: httpx.Client, from_date: str | None = None, to_date: str | None = None
) -> tuple[list[HistoricalRelease], int]:
    """Continuous CDX resumeKey walk, optionally bounded to [from_date, to_date]
    (YYYYMMDD, inclusive -- CDX's own capture-time filter)."""
    releases: list[HistoricalRelease] = []
    seen = 0
    resume_key: str | None = None

    while len(releases) < limit:
        params = {
            "url": "businesswire.com/news/home",
            "matchType": "prefix",
            "output": "json",
            "filter": "statuscode:200",
            "collapse": "urlkey",
            "fl": "timestamp,original",
            "limit": WAYBACK_PAGE_SIZE,
            "showResumeKey": "true",
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if resume_key:
            params["resumeKey"] = resume_key

        try:
            response = http_client.get(WAYBACK_CDX_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("wayback_page_failed resume_key=%s error=%s", resume_key, exc)
            break

        if not data:
            break

        rows = data[1:]  # drop the header row ("timestamp", "original")
        resume_key = None
        if rows and len(rows[-1]) == 1:
            resume_key = rows[-1][0]
            rows = rows[:-1]

        for _timestamp, original in rows:
            if len(releases) >= limit:
                break
            seen += 1
            release = _parse_wayback_row(original)
            if release is not None:
                releases.append(release)

        time.sleep(WAYBACK_DELAY_SECONDS)

        if not resume_key:
            break

    return releases, seen


def fetch_wayback_businesswire(
    limit: int,
    http_client: httpx.Client,
    per_period_limit: int | None = DEFAULT_PER_PERIOD_LIMIT,
    start_year: int = WAYBACK_START_YEAR,
    end_year: int | None = None,
) -> tuple[list[HistoricalRelease], int]:
    """per_period_limit caps how many releases any single YEAR can contribute
    (None = unlimited, i.e. the old continuous-walk behavior, which stays
    within the first few months of start_year for any realistic --limit).
    Default is capped so the walk spreads across every year in range."""
    if per_period_limit is None:
        return _fetch_wayback_range(limit, http_client)

    end_year = end_year or datetime.now(UTC).year
    releases: list[HistoricalRelease] = []
    seen = 0

    for year in range(start_year, end_year + 1):
        if len(releases) >= limit:
            break
        year_budget = min(per_period_limit, limit - len(releases))
        year_releases, year_seen = _fetch_wayback_range(
            year_budget, http_client, from_date=f"{year}0101", to_date=f"{year}1231"
        )
        releases.extend(year_releases)
        seen += year_seen

    return releases, seen


# --- upsert + CLI ------------------------------------------------------------

SOURCE_FETCHERS = {
    "globenewswire-sitemap": fetch_globenewswire,
    "wayback-businesswire": fetch_wayback_businesswire,
}


def _dedupe_by_url(releases: list[HistoricalRelease]) -> list[HistoricalRelease]:
    # Postgres rejects a single ON CONFLICT DO UPDATE batch that contains two
    # rows with the same conflict key ("cannot affect row a second time") --
    # the whole batch is rejected atomically, nothing gets inserted. This can
    # happen even though a single CDX query already collapses by urlkey: the
    # per-year spread (fetch_wayback_businesswire's per_period_limit path)
    # queries each year independently, and the same underlying release can
    # surface as the representative capture in two adjacent years' results
    # when its capture history straddles a year boundary.
    seen: set[str] = set()
    deduped = []
    for release in releases:
        if release.url in seen:
            continue
        seen.add(release.url)
        deduped.append(release)
    return deduped


def _upsert(client, releases: list[HistoricalRelease]) -> int:
    releases = _dedupe_by_url(releases)
    if not releases:
        return 0
    rows = [r.model_dump(mode="json") for r in releases]
    # on_conflict="url" matches releases.url (UNIQUE) so re-running never
    # inserts duplicates -- same pattern as src/ingest/pollers.py.
    client.table("releases").upsert(rows, on_conflict="url").execute()
    return len(rows)


def run(
    source: str | None,
    limit: int,
    month: str | None = None,
    per_period_limit: int | None = DEFAULT_PER_PERIOD_LIMIT,
) -> None:
    client = get_client()
    sources = [source] if source else list(SOURCE_FETCHERS)

    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http_client:
        for src in sources:
            try:
                if src == "globenewswire-sitemap":
                    releases, pulled = fetch_globenewswire(
                        limit, http_client, month=month, per_period_limit=per_period_limit
                    )
                else:
                    if month:
                        logger.warning(
                            "month_ignored source=%s -- --month only applies to globenewswire-sitemap", src
                        )
                    releases, pulled = fetch_wayback_businesswire(
                        limit, http_client, per_period_limit=per_period_limit
                    )
                inserted = _upsert(client, releases)
                logger.info(
                    "source_done source=%s pulled=%d inserted=%d skipped=%d",
                    src, pulled, inserted, pulled - inserted,
                )
            except Exception:
                logger.exception("source_failed source=%s", src)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical release metadata (Track B1).")
    parser.add_argument(
        "--source", choices=list(SOURCE_FETCHERS), default=None,
        help="Limit to one source; default runs both.",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Max releases to pull per source (default {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--month", default=None, metavar="YYYY-MM",
        help="GlobeNewswire only: fetch this specific month's sitemap directly, "
             "for deterministic historical spot-checks (e.g. --month 2023-09).",
    )
    parser.add_argument(
        "--per-period-limit", type=int, default=DEFAULT_PER_PERIOD_LIMIT,
        help="Cap releases pulled from any single period (month for GlobeNewswire, year for "
             f"Wayback) before moving on, so the walk spreads across time (default {DEFAULT_PER_PERIOD_LIMIT}). "
             "Pass 0 to disable capping and exhaust periods consecutively (the old behavior).",
    )
    args = parser.parse_args()
    per_period_limit = None if args.per_period_limit == 0 else args.per_period_limit
    run(args.source, args.limit, month=args.month, per_period_limit=per_period_limit)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
