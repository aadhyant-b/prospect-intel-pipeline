from types import SimpleNamespace

import src.ingest.backfill_live_company_names as backfill


class _FakeSelectQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
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
        self._id = None

    def eq(self, _col, value):
        self._id = value
        return self

    def execute(self):
        self._recorder.append((self._id, self._payload))
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


def test_regex_resolved_row_never_calls_haiku(monkeypatch):
    rows = [{"id": "r1", "title": "Acme Corp Announces $10M Series A"}]
    fake_client = FakeClient(rows)
    monkeypatch.setattr(backfill, "get_client", lambda: fake_client)

    def _boom(*a, **k):
        raise AssertionError("Haiku should not be called when regex resolves the name")

    monkeypatch.setattr(backfill, "extract_company_name", _boom)
    monkeypatch.setattr(backfill.anthropic, "Anthropic", lambda: object())

    backfill.run(limit=None, use_haiku=True)

    assert fake_client.updates == [("r1", {"company_name_raw": "Acme Corp"})]


def test_junk_title_skipped_without_calling_haiku(monkeypatch):
    rows = [{"id": "r1", "title": "Gtm.js"}]
    fake_client = FakeClient(rows)
    monkeypatch.setattr(backfill, "get_client", lambda: fake_client)
    monkeypatch.setattr(
        backfill, "extract_company_name",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call Haiku for junk titles")),
    )
    monkeypatch.setattr(backfill.anthropic, "Anthropic", lambda: object())

    backfill.run(limit=None, use_haiku=True)

    assert fake_client.updates == [("r1", {"company_name_raw": ""})]


def test_regex_miss_falls_back_to_haiku(monkeypatch):
    rows = [{"id": "r1", "title": "Robert Bosch GmbH IAM Express Confidence Tele"}]
    fake_client = FakeClient(rows)
    monkeypatch.setattr(backfill, "get_client", lambda: fake_client)
    monkeypatch.setattr(backfill, "extract_company_name", lambda client, title: ("Robert Bosch GmbH", 0.0004))
    monkeypatch.setattr(backfill.anthropic, "Anthropic", lambda: object())

    backfill.run(limit=None, use_haiku=True)

    assert fake_client.updates == [("r1", {"company_name_raw": "Robert Bosch GmbH"})]


def test_regex_miss_confirmed_no_company_via_haiku(monkeypatch):
    # Regex returns None here (colon span exceeds the word-count gate, no
    # verb match either) -- genuinely exercises the Haiku-fallback path,
    # unlike a title regex could already resolve.
    rows = [{"id": "r1", "title": "The Soccer League That Never Stops: LegaBot, the World's First AI Referee"}]
    fake_client = FakeClient(rows)
    monkeypatch.setattr(backfill, "get_client", lambda: fake_client)
    monkeypatch.setattr(backfill, "extract_company_name", lambda client, title: (None, 0.0003))
    monkeypatch.setattr(backfill.anthropic, "Anthropic", lambda: object())

    backfill.run(limit=None, use_haiku=True)

    assert fake_client.updates == [("r1", {"company_name_raw": ""})]


def test_no_haiku_flag_leaves_regex_misses_untouched(monkeypatch):
    rows = [{"id": "r1", "title": "Robert Bosch GmbH IAM Express Confidence Tele"}]
    fake_client = FakeClient(rows)
    monkeypatch.setattr(backfill, "get_client", lambda: fake_client)

    backfill.run(limit=None, use_haiku=False)

    assert fake_client.updates == []
