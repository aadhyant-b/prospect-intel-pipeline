import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from src.ingest.historical import (
    SITEMAP_NS,
    _guess_company_name,
    _parse_globenewswire_entry,
    _parse_wayback_row,
    _slug_to_title,
)


def test_guess_company_name_colon_style():
    assert _guess_company_name("Zelluna ASA: Disclosure of large shareholding") == "Zelluna ASA"


def test_guess_company_name_verb_style():
    assert _guess_company_name("Acme Corp Announces $10M Series A") == "Acme Corp"


def test_guess_company_name_no_match_returns_none():
    assert _guess_company_name("A Report on the State of the Industry") is None


def test_guess_company_name_empty_string():
    assert _guess_company_name("") is None


def test_slug_to_title():
    assert (
        _slug_to_title("sephora-announces-2020-birthday-gift-offerings-beauty")
        == "Sephora announces 2020 birthday gift offerings beauty"
    )


def test_slug_to_title_empty():
    assert _slug_to_title("") == ""


def test_parse_wayback_row_extracts_date_and_slug():
    url = "https://www.businesswire.com/news/home/20030317005056/en/LeftHand-Networks-Secures-20-Million-Series-Financing"
    release = _parse_wayback_row(url)
    assert release is not None
    assert release.published_at == datetime(2003, 3, 17, tzinfo=UTC)
    assert release.distributor == "Business Wire"
    assert release.source == "wayback-businesswire"
    assert "LeftHand Networks" in release.title
    assert release.company_name_raw == "LeftHand Networks"


def test_parse_wayback_row_no_slug():
    url = "https://www.businesswire.com/news/home/20211224005817/en/"
    release = _parse_wayback_row(url)
    assert release is not None
    assert release.published_at == datetime(2021, 12, 24, tzinfo=UTC)
    assert release.title == "(untitled Business Wire release)"


def test_parse_wayback_row_unparseable_url_returns_none():
    assert _parse_wayback_row("https://www.businesswire.com/portal/site/home/") is None


def test_parse_globenewswire_entry():
    xml = """
    <url xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
         xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <loc>https://www.globenewswire.com/news-release/2026/08/04/1/en/Example.html</loc>
      <news:news>
        <news:publication><news:name>GlobeNewswire</news:name><news:language>en</news:language></news:publication>
        <news:genres>PressRelease</news:genres>
        <news:publication_date>2026-08-04T15:23:05+00:00</news:publication_date>
        <news:title>Acme Corp Raises $10 Million Series A</news:title>
      </news:news>
    </url>
    """
    url_elem = ET.fromstring(xml)
    release = _parse_globenewswire_entry(url_elem)
    assert release is not None
    assert release.url == "https://www.globenewswire.com/news-release/2026/08/04/1/en/Example.html"
    assert release.distributor == "GlobeNewswire"
    assert release.source == "globenewswire-sitemap"
    assert release.company_name_raw == "Acme Corp"
    assert release.published_at.year == 2026


def test_parse_globenewswire_entry_missing_fields_returns_none():
    xml = """
    <url xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <loc>https://www.globenewswire.com/no-news-block.html</loc>
    </url>
    """
    url_elem = ET.fromstring(xml)
    assert _parse_globenewswire_entry(url_elem) is None


def test_sitemap_ns_importable():
    # Sanity check the module's namespace map matches the real sitemap schema.
    assert SITEMAP_NS["sm"] == "http://www.sitemaps.org/schemas/sitemap/0.9"


# Regression cases for real bad guesses seen in a 150-title production sample.
def test_guess_company_name_rejects_long_verb_clause():
    # Real title: "Minuteman Press Franchise Owner Ken Holloway Achieves
    # President's Club for ..." -- pre-verb span is 6 words, not a company.
    assert _guess_company_name("Minuteman Press Franchise Owner Ken Holloway Achieves President's Club") is None


def test_guess_company_name_rejects_tagline_before_colon():
    # Real title: "The Soccer League That Never Stops: LegaBot, the World's
    # First ..." -- the real subject (LegaBot) is AFTER the colon; the
    # pre-colon span is a 6-word tagline, not a company name.
    assert _guess_company_name("The Soccer League That Never Stops: LegaBot, the World's First AI Referee") is None


def test_guess_company_name_falls_back_to_verb_when_colon_span_too_long():
    # Real title: "Volaris Reports July 2026 Traffic Results: Load Factor of
    # 88%" -- colon span is 6 words (rejected), but the verb split correctly
    # isolates "Volaris".
    assert _guess_company_name("Volaris Reports July 2026 Traffic Results: Load Factor of 88%") == "Volaris"


def test_looks_like_company_name_word_count_boundary():
    from src.ingest.company_name_heuristic import MAX_COMPANY_NAME_WORDS, _looks_like_company_name

    assert _looks_like_company_name(" ".join(["Word"] * MAX_COMPANY_NAME_WORDS)) is True
    assert _looks_like_company_name(" ".join(["Word"] * (MAX_COMPANY_NAME_WORDS + 1))) is False


def test_fetch_globenewswire_walks_oldest_first_by_default(monkeypatch):
    from src.ingest import historical

    monkeypatch.setattr(
        historical, "_fetch_globenewswire_sitemap_urls",
        lambda http_client: ["https://sitemaps.../2026-08.xml", "https://sitemaps.../2023-09.xml"],
    )
    fetched_order = []

    class FakeResponse:
        content = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, timeout=None):
            fetched_order.append(url)
            return FakeResponse()

    historical.fetch_globenewswire(limit=1000, http_client=FakeClient())
    assert fetched_order == ["https://sitemaps.../2023-09.xml", "https://sitemaps.../2026-08.xml"]


def test_fetch_globenewswire_month_bypasses_index(monkeypatch):
    from src.ingest import historical

    def _fail(*args, **kwargs):
        raise AssertionError("index should not be fetched when --month is given")

    monkeypatch.setattr(historical, "_fetch_globenewswire_sitemap_urls", _fail)
    fetched_order = []

    class FakeResponse:
        content = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, timeout=None):
            fetched_order.append(url)
            return FakeResponse()

    historical.fetch_globenewswire(limit=10, http_client=FakeClient(), month="2023-09")
    assert fetched_order == ["https://sitemaps.globenewswire.com/news/en/2023-09.xml"]


def test_guess_company_name_does_not_split_on_partners_inside_a_name():
    # Real title: "Brookfield Business Partners L.P. 2023 Third Quarter
    # Conference Call ..." -- "partners" is deliberately excluded from the
    # verb list because it's a common word inside real company names.
    assert _guess_company_name("Brookfield Business Partners L.P. Announces Third Quarter Call") == (
        "Brookfield Business Partners L.P."
    )


def test_guess_company_name_rejects_wire_notice_prefixes():
    # Real titles: "CORRECTION: Ascentage Pharma to Report ..." and
    # "Update: The Apache Software Foundation Announces ...".
    assert _guess_company_name("CORRECTION: Ascentage Pharma to Report Results") is None
    assert _guess_company_name("Update: The Apache Software Foundation Announces Gradle") is None


def test_guess_company_name_strips_trailing_verb_before_colon():
    # Real title: "Hemp, Inc. Reports: Growing Legalization of Industrial
    # Hemp Driving Global ..." -- colon-split alone would include "Reports".
    assert _guess_company_name("Hemp, Inc. Reports: Growing Legalization of Industrial Hemp") == "Hemp, Inc."


def _gnw_month_xml(n_entries: int) -> bytes:
    urls = "".join(
        f"""<url>
              <loc>https://www.globenewswire.com/news-release/x/{i}.html</loc>
              <news:news>
                <news:publication_date>2024-01-0{(i % 9) + 1}T00:00:00+00:00</news:publication_date>
                <news:title>Company{i} Announces Update {i}</news:title>
              </news:news>
            </url>"""
        for i in range(n_entries)
    )
    return (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        f"{urls}</urlset>"
    ).encode()


def test_fetch_globenewswire_caps_releases_per_month(monkeypatch):
    from src.ingest import historical

    monkeypatch.setattr(
        historical, "_fetch_globenewswire_sitemap_urls",
        lambda http_client: ["https://sitemaps.../2024-01.xml", "https://sitemaps.../2024-02.xml"],
    )

    class FakeResponse:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, timeout=None):
            return FakeResponse(_gnw_month_xml(50))  # 50 entries available per month

    releases, seen = historical.fetch_globenewswire(
        limit=1000, http_client=FakeClient(), per_period_limit=10
    )

    # 2 months (oldest-first order reversed -> both fetched), capped at 10 each.
    assert len(releases) == 20
    assert seen == 20  # capped before examining the rest of each month's entries


def test_fetch_globenewswire_uncapped_when_per_period_limit_none(monkeypatch):
    from src.ingest import historical

    monkeypatch.setattr(
        historical, "_fetch_globenewswire_sitemap_urls",
        lambda http_client: ["https://sitemaps.../2024-01.xml", "https://sitemaps.../2024-02.xml"],
    )

    class FakeResponse:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, timeout=None):
            return FakeResponse(_gnw_month_xml(50))

    releases, seen = historical.fetch_globenewswire(
        limit=30, http_client=FakeClient(), per_period_limit=None
    )

    # Old behavior: the first month alone satisfies the overall limit.
    assert len(releases) == 30
    assert seen == 30


def test_fetch_wayback_businesswire_walks_years_with_per_period_cap(monkeypatch):
    from src.ingest import historical

    requested_ranges = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def get(self, url, params=None, timeout=None):
            requested_ranges.append((params.get("from"), params.get("to")))
            year = params["from"][:4]
            rows = [["timestamp", "original"]]
            for i in range(5):
                rows.append([
                    f"{year}0101000000",
                    f"https://www.businesswire.com/news/home/{year}010100{i:04d}/en/Company-{year}-{i}",
                ])
            return FakeResponse(rows)

    releases, seen = historical.fetch_wayback_businesswire(
        limit=1000, http_client=FakeClient(),
        per_period_limit=3, start_year=2020, end_year=2022,
    )

    # 3 years walked, capped at 3 releases each -> 9 total, and each request
    # carried that year's from/to bounds (proving the spread, not one query).
    assert len(releases) == 9
    assert seen == 9
    assert requested_ranges == [("20200101", "20201231"), ("20210101", "20211231"), ("20220101", "20221231")]
    assert {r.published_at.year for r in releases} == {2020, 2021, 2022}


def test_dedupe_by_url_keeps_first_occurrence():
    from src.ingest.historical import HistoricalRelease, _dedupe_by_url

    a = HistoricalRelease(
        url="https://example.com/x", title="A", distributor="Business Wire",
        published_at=datetime(2020, 1, 1, tzinfo=UTC), source="wayback-businesswire",
        company_name_raw="Acme",
    )
    # Same URL surfacing again (e.g. from an adjacent year's CDX query) --
    # must be dropped, not passed through to a duplicate-key upsert batch.
    b = HistoricalRelease(
        url="https://example.com/x", title="A duplicate", distributor="Business Wire",
        published_at=datetime(2020, 1, 1, tzinfo=UTC), source="wayback-businesswire",
        company_name_raw="Acme",
    )
    c = HistoricalRelease(
        url="https://example.com/y", title="B", distributor="Business Wire",
        published_at=datetime(2021, 1, 1, tzinfo=UTC), source="wayback-businesswire",
        company_name_raw="Beta",
    )

    deduped = _dedupe_by_url([a, b, c])

    assert [r.url for r in deduped] == ["https://example.com/x", "https://example.com/y"]
    assert deduped[0].title == "A"  # first occurrence wins
