from types import SimpleNamespace

import anthropic
import httpx
from src.extract import company_backfill
from src.extract.company_backfill import CompanyNameResult, _should_skip


class _FakeSelectQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def neq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeUpdateQuery:
    def __init__(self, recorder, payload):
        self._recorder = recorder
        self._payload = payload

    def eq(self, _col, value):
        self._recorder.append((value, self._payload))
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeTable:
    def __init__(self, rows, updates):
        self._rows = rows
        self._updates = updates

    def select(self, *a, **k):
        return _FakeSelectQuery(self._rows)

    def update(self, payload):
        return _FakeUpdateQuery(self._updates, payload)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []

    def table(self, _name):
        return _FakeTable(self.rows, self.updates)


def test_should_skip_dotted_token_titles():
    assert _should_skip("Gtm.js") is True
    assert _should_skip("Gtm.start") is True


def test_should_skip_placeholder_title():
    assert _should_skip("(untitled Business Wire release)") is True


def test_should_skip_false_for_real_headline():
    assert _should_skip("Robert Bosch GmbH IAM Express Confidence Tele") is False


def test_company_name_result_accepts_null():
    result = CompanyNameResult(company_name=None)
    assert result.company_name is None


def test_company_name_result_accepts_name():
    result = CompanyNameResult(company_name="Robert Bosch GmbH")
    assert result.company_name == "Robert Bosch GmbH"


def test_run_continues_past_a_single_api_error(monkeypatch):
    # A malformed/unusual title triggering a 400 must not kill the whole
    # batch -- the next row should still be processed, and the failing row
    # left untouched (not marked '' -- that would wrongly claim "confirmed
    # no company" for a row we never actually got an answer for).
    rows = [
        {"id": "bad", "title": "Some Malformed Title"},
        {"id": "good", "title": "Acme Corp Announces $10M Series A"},
    ]
    fake_client = FakeClient(rows)
    monkeypatch.setattr(company_backfill, "get_client", lambda: fake_client)
    monkeypatch.setattr(company_backfill, "anthropic", SimpleNamespace(Anthropic=lambda: object(), APIError=anthropic.APIError))

    call_count = 0

    def fake_extract(client, title):
        nonlocal call_count
        call_count += 1
        if title == "Some Malformed Title":
            raise anthropic.APIError("Invalid request data", request=httpx.Request("POST", "https://api.anthropic.com"), body=None)
        return "Acme Corp", 0.0004

    monkeypatch.setattr(company_backfill, "extract_company_name", fake_extract)

    company_backfill.run(source=None, limit=10)

    assert call_count == 2  # both rows attempted, not just the first
    assert fake_client.updates == [("good", {"company_name_raw": "Acme Corp"})]
