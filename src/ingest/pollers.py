"""RSS pollers: fetch press releases from wire-service feeds and upsert them."""

import calendar
import logging
import time
from datetime import UTC, datetime

import feedparser
import httpx
from pydantic import BaseModel, ValidationError

from src.db import get_client

logger = logging.getLogger(__name__)

# Distributor name -> public RSS feed URL. Add new wires here; nothing else
# needs to change to start polling them — it's a one-line dict entry.
#
# Business Wire investigation (excluded — no working public feed found):
#   ~13 candidate URLs were tried across www.businesswire.com and
#   feed.businesswire.com. Findings:
#     - www.businesswire.com (where real category/industry feed links would
#       be discoverable) does not respond to non-browser HTTP requests at
#       all — every path times out, consistent with bot-blocking.
#     - feed.businesswire.com does respond, but every "?rss=<token>" value
#       tried (the one previously in this dict, plus guessed variants)
#       returns an identical empty channel ("RSS channel ID is not
#       available"). That token appears to be a per-subscriber saved-search
#       ID issued through their authenticated portal, not a guessable public
#       category code — i.e. Business Wire's RSS is not a general public
#       feed in the way PR Newswire's and GlobeNewswire's are.
#   ACCESS Newswire, EIN Presswire, and PRWeb were also tried and rejected:
#   ACCESS Newswire is behind a Cloudflare bot challenge (403 on every
#   path); EIN Presswire's RSS is per-newsroom-ID only, no general feed
#   found; PRWeb 404s on every path tried.
#
# Funding-density investigation (2026-08-01): the general feeds above are
# dominated by micro-cap/mining/public-company noise, not venture funding.
# PR Newswire exposes a real category taxonomy (discovered via their /rss/
# index page) — "financial-services-latest-news/venture-capital-list.rss"
# tested at ~50% genuine VC-round density (Series A-D, real $ amounts),
# added below. GlobeNewswire has no equivalent "Venture Capital" subject
# code, only "fin" = Financing Agreements
# (/RssFeed/subject/fin/feedTitle/...) — tested twice, ~5% density (1
# genuine hit in 20, rest micro-cap private placements and debt/credit
# facilities, plus duplicate multi-language entries of the same story).
# Same noise profile as PR Newswire's private-placement-list; skipped for
# the same reason. Neither wire's feeds support pagination (?page=,
# ?startDate=, etc. are silently ignored) — for real historical backfill,
# see the sitemap-walking investigation instead (not yet built).
FEEDS: dict[str, str] = {
    # General "all news releases" feed — confirmed stable public endpoint.
    # Kept for the switch detector's broader coverage despite low funding
    # density.
    "PR Newswire": "https://www.prnewswire.com/rss/news-releases-list.rss",
    # GlobeNewswire's general newsroom feed (org class 1 = all organizations).
    # Kept for the same reason.
    "GlobeNewswire": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20Room",
    # High-density funding category feed — ~50% genuine VC rounds per
    # testing above.
    "PR Newswire - Venture Capital": "https://www.prnewswire.com/rss/financial-services-latest-news/venture-capital-list.rss",
}

USER_AGENT = "press-release-intel-bot/0.1 (+https://github.com/aadhyant-b/prospect-intel-pipeline)"
REQUEST_TIMEOUT_SECONDS = 15
DELAY_BETWEEN_FEEDS_SECONDS = 2.0


class Release(BaseModel):
    url: str
    title: str
    distributor: str
    published_at: datetime
    raw_text: str


def _entry_to_release(entry: feedparser.FeedParserDict, distributor: str) -> Release | None:
    url = entry.get("link")
    title = entry.get("title")
    raw_text = entry.get("summary") or entry.get("description") or ""

    published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    # feedparser normalizes *_parsed to UTC, so use timegm (not mktime, which
    # assumes local time) to convert it back to an epoch timestamp correctly.
    published_at = (
        datetime.fromtimestamp(calendar.timegm(published_struct), tz=UTC)
        if published_struct
        else None
    )

    try:
        return Release(
            url=url,
            title=title,
            distributor=distributor,
            published_at=published_at,
            raw_text=raw_text,
        )
    except ValidationError as exc:
        logger.warning(
            "skipped invalid entry distributor=%s url=%r error=%s",
            distributor,
            url,
            exc,
        )
        return None


def _fetch_feed(distributor: str, feed_url: str) -> list[Release]:
    response = httpx.get(
        feed_url,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()

    parsed = feedparser.parse(response.content)
    releases = []
    skipped = 0
    for entry in parsed.entries:
        release = _entry_to_release(entry, distributor)
        if release is None:
            skipped += 1
            continue
        releases.append(release)

    logger.info(
        "feed_parsed distributor=%s seen=%d valid=%d skipped=%d",
        distributor,
        len(parsed.entries),
        len(releases),
        skipped,
    )
    return releases


def _upsert_releases(client, releases: list[Release]) -> int:
    if not releases:
        return 0
    rows = [r.model_dump(mode="json") for r in releases]
    # on_conflict="url" matches incoming rows against releases.url (UNIQUE)
    # and updates in place instead of inserting a duplicate row.
    client.table("releases").upsert(rows, on_conflict="url").execute()
    return len(rows)


def poll_all() -> None:
    client = get_client()
    feed_items = list(FEEDS.items())

    for i, (distributor, feed_url) in enumerate(feed_items):
        try:
            releases = _fetch_feed(distributor, feed_url)
            inserted = _upsert_releases(client, releases)
            logger.info(
                "feed_done distributor=%s seen=%d inserted=%d skipped=%d",
                distributor,
                len(releases),
                inserted,
                len(releases) - inserted,
            )
        except Exception:
            logger.exception("feed_failed distributor=%s url=%s", distributor, feed_url)

        if i < len(feed_items) - 1:
            time.sleep(DELAY_BETWEEN_FEEDS_SECONDS)
