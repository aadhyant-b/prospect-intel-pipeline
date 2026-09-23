from types import SimpleNamespace

from src.extract import leads_ingester
from src.extract.extractor import GroundedExtractionResult


class _FakeQuery:
    def __init__(self, table_name, recorder):
        self._table_name = table_name
        self._recorder = recorder
        self._eq_filters = {}
        self._in_filters = {}
        self._range = None
        self._insert_payload = None
        self._upsert_payload = None
        self._on_conflict = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq_filters[col] = val
        return self

    def in_(self, col, values):
        self._in_filters[col] = set(values)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def insert(self, payload):
        self._insert_payload = payload
        self._recorder.inserts.setdefault(self._table_name, []).append(payload)
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert_payload = payload
        self._on_conflict = on_conflict
        self._recorder.upserts.setdefault(self._table_name, []).append((payload, on_conflict))
        return self

    def execute(self):
        if self._insert_payload is not None:
            row = dict(self._insert_payload)
            row.setdefault("id", f"ext-{len(self._recorder.inserts[self._table_name])}")
            return SimpleNamespace(data=[row])
        if self._upsert_payload is not None:
            return SimpleNamespace(data=[dict(self._upsert_payload)])

        data = self._recorder.select_data.get(self._table_name, [])
        for col, val in self._eq_filters.items():
            data = [r for r in data if r.get(col) == val]
        for col, vals in self._in_filters.items():
            data = [r for r in data if r.get(col) in vals]
        if self._range:
            start, end = self._range
            data = data[start : end + 1]
        return SimpleNamespace(data=data)


class FakeClient:
    def __init__(self, select_data):
        self.select_data = select_data
        self.inserts: dict[str, list[dict]] = {}
        self.upserts: dict[str, list[tuple]] = {}

    def table(self, name):
        return _FakeQuery(name, self)


def _grounded(**overrides):
    defaults = {
        "is_funding_related": True,
        "company_name": "Acme Corp",
        "funding_round": "series-a",
        "amount_usd": 10_000_000.0,
        "investors": ["Sequoia Capital"],
        "sector": "Fintech",
        "grounding_failures": [],
    }
    defaults.update(overrides)
    return GroundedExtractionResult(**defaults)


def test_fetch_processed_release_ids_paginates(monkeypatch):
    monkeypatch.setattr(leads_ingester, "_FETCH_PAGE_SIZE", 2)
    rows = [{"release_id": f"r{i}"} for i in range(5)]
    client = FakeClient({"extractions": rows})

    ids = leads_ingester._fetch_processed_release_ids(client)

    assert ids == {f"r{i}" for i in range(5)}


def test_fetch_candidate_releases_skips_already_processed_and_respects_limit(monkeypatch):
    monkeypatch.setattr(leads_ingester, "_FETCH_PAGE_SIZE", 10)
    source = "PR Newswire - Venture Capital"
    # Known _title_score values (verified against _TITLE_KEYWORDS / _AMOUNT_RE):
    #   r_amt:      "raises" (1) + explicit $ amount (+2) = 3
    #   r_tie_new:  "closes" (1), published_at 2026-01-08
    #   r_tie_old:  "closes" (1), published_at 2026-01-02  (same score as r_tie_new, older -> ranks after)
    #   r_zero:     no keyword hits = 0
    #   r_processed: already in extractions -- must be excluded regardless of score
    releases = [
        {"id": "r_zero", "title": "Foo announces new office", "raw_text": "x", "distributor": source, "published_at": "2026-01-10"},
        {"id": "r_amt", "title": "Foo raises $5 million in new round", "raw_text": "x", "distributor": source, "published_at": "2026-01-01"},
        {"id": "r_tie_old", "title": "Foo closes acquisition deal", "raw_text": "x", "distributor": source, "published_at": "2026-01-02"},
        {"id": "r_tie_new", "title": "Bar closes acquisition deal", "raw_text": "x", "distributor": source, "published_at": "2026-01-08"},
        {"id": "r_processed", "title": "Baz raises $50 million", "raw_text": "x", "distributor": source, "published_at": "2026-01-09"},
    ]
    client = FakeClient({
        "extractions": [{"release_id": "r_processed"}],
        "releases": releases,
    })

    # Sanity-check the assumed scores so the test fails loudly (not silently)
    # if _title_score's keyword list or amount regex ever changes.
    assert leads_ingester._title_score("Foo raises $5 million in new round") == 3
    assert leads_ingester._title_score("Foo closes acquisition deal") == 1
    assert leads_ingester._title_score("Bar closes acquisition deal") == 1
    assert leads_ingester._title_score("Foo announces new office") == 0

    candidates = leads_ingester._fetch_candidate_releases(client, source, limit=3)

    # Ranked by score desc, ties broken by published_at desc (more recent first);
    # r_processed is excluded despite its high score, r_zero excluded by the limit.
    assert [r["id"] for r in candidates] == ["r_amt", "r_tie_new", "r_tie_old"]


def test_run_dry_run_writes_nothing(monkeypatch):
    client = FakeClient({"extractions": [], "releases": [
        {"id": "r1", "title": "Acme raises $10M", "raw_text": "x", "distributor": "PR Newswire - Venture Capital", "published_at": "2026-01-01"},
    ]})
    monkeypatch.setattr(leads_ingester, "get_client", lambda: client)
    monkeypatch.setattr(leads_ingester, "anthropic", SimpleNamespace(Anthropic=lambda: object()))
    monkeypatch.setattr(leads_ingester, "extract_release", lambda *a, **k: (_grounded(), 0.001))

    leads_ingester.run(source="PR Newswire - Venture Capital", limit=10, write=False)

    assert client.inserts == {}
    assert client.upserts == {}


def test_run_write_inserts_extraction_and_lead(monkeypatch):
    client = FakeClient({"extractions": [], "releases": [
        {"id": "r1", "title": "Acme raises $10M", "raw_text": "x", "distributor": "PR Newswire - Venture Capital", "published_at": "2026-01-01T00:00:00Z"},
    ]})
    monkeypatch.setattr(leads_ingester, "get_client", lambda: client)
    monkeypatch.setattr(leads_ingester, "anthropic", SimpleNamespace(Anthropic=lambda: object()))
    monkeypatch.setattr(leads_ingester, "extract_release", lambda *a, **k: (_grounded(), 0.001))

    leads_ingester.run(source="PR Newswire - Venture Capital", limit=10, write=True)

    assert len(client.inserts["extractions"]) == 1
    assert client.inserts["extractions"][0]["release_id"] == "r1"

    assert len(client.upserts["funding_leads"]) == 1
    lead_payload, on_conflict = client.upserts["funding_leads"][0]
    assert on_conflict == "release_id"
    assert lead_payload["release_id"] == "r1"
    assert lead_payload["company_group_key"] == "acme"
    assert lead_payload["company_name"] == "Acme Corp"
    assert lead_payload["fully_grounded"] is True
    assert lead_payload["extraction_id"] == "ext-1"


def test_run_not_funding_related_only_logs_extraction(monkeypatch):
    client = FakeClient({"extractions": [], "releases": [
        {"id": "r1", "title": "Routine news", "raw_text": "x", "distributor": "PR Newswire - Venture Capital", "published_at": "2026-01-01T00:00:00Z"},
    ]})
    monkeypatch.setattr(leads_ingester, "get_client", lambda: client)
    monkeypatch.setattr(leads_ingester, "anthropic", SimpleNamespace(Anthropic=lambda: object()))
    monkeypatch.setattr(
        leads_ingester, "extract_release",
        lambda *a, **k: (_grounded(is_funding_related=False, company_name=None, funding_round=None, amount_usd=None, investors=[], sector=None), 0.001),
    )

    leads_ingester.run(source="PR Newswire - Venture Capital", limit=10, write=True)

    assert len(client.inserts["extractions"]) == 1
    assert "funding_leads" not in client.upserts


def test_run_skips_lead_when_company_name_ungrounded(monkeypatch):
    # is_funding_related=True but grounding already nulled company_name --
    # the attempt is logged, but no funding_leads row should be written.
    client = FakeClient({"extractions": [], "releases": [
        {"id": "r1", "title": "Startup raises funding", "raw_text": "x", "distributor": "PR Newswire - Venture Capital", "published_at": "2026-01-01T00:00:00Z"},
    ]})
    monkeypatch.setattr(leads_ingester, "get_client", lambda: client)
    monkeypatch.setattr(leads_ingester, "anthropic", SimpleNamespace(Anthropic=lambda: object()))
    monkeypatch.setattr(
        leads_ingester, "extract_release",
        lambda *a, **k: (_grounded(company_name=None, grounding_failures=["company_name: 'Fake Co' not found in source text"]), 0.001),
    )

    leads_ingester.run(source="PR Newswire - Venture Capital", limit=10, write=True)

    assert len(client.inserts["extractions"]) == 1
    assert "funding_leads" not in client.upserts
