from fastapi.testclient import TestClient
from src.app import main


def test_health():
    client = TestClient(main.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_redirects_to_leads():
    client = TestClient(main.app, follow_redirects=False)
    response = client.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "/leads"


def _fake_leads_list_row(**overrides):
    row = {
        "id": "lead-1",
        "company_name": "Acme Corp",
        "funding_round": "series-a",
        "amount_usd": 10_000_000.0,
        "amount_usd_fmt": "$10.0M",
        "sector": "Fintech",
        "published_at": "2026-08-01T00:00:00Z",
        "fully_grounded": True,
        "bar_pct": 50,
        "switch_signal": None,
        "investors": ["A Ventures"],
        "investor_count": 1,
        "time_ago": "2d ago",
        "ticker": "ACME",
        "activity": {"kind": "none", "text": "—", "detail": ""},
    }
    row.update(overrides)
    return row


def test_leads_list_renders_rows(monkeypatch):
    fake_leads = [_fake_leads_list_row()]
    monkeypatch.setattr(main, "fetch_leads", lambda: fake_leads)

    client = TestClient(main.app)
    response = client.get("/leads")

    assert response.status_code == 200
    assert "Acme Corp" in response.text
    assert "$10.0M" in response.text
    assert "VERIFIED" in response.text
    assert "lead-1" in response.text
    assert "ACME" in response.text  # ticker code
    assert "2d ago" in response.text


def test_leads_list_stat_bar_reflects_totals(monkeypatch):
    fake_leads = [
        _fake_leads_list_row(id="lead-1", amount_usd=10_000_000.0, fully_grounded=True),
        _fake_leads_list_row(id="lead-2", amount_usd=5_000_000.0, fully_grounded=False),
    ]
    monkeypatch.setattr(main, "fetch_leads", lambda: fake_leads)

    client = TestClient(main.app)
    response = client.get("/leads")

    assert response.status_code == 200
    assert "$15.0M" in response.text  # total capital tracked
    assert "1/2" in response.text  # verified count


def test_leads_list_shows_pulsing_activity_signal(monkeypatch):
    fake_leads = [_fake_leads_list_row(
        switch_signal={"score": 0.8, "reasons": ["Gone quiet: 90 days"]},
        activity={"kind": "signal", "text": "⚡ ACTIVITY", "detail": "Gone quiet: 90 days"},
    )]
    monkeypatch.setattr(main, "fetch_leads", lambda: fake_leads)

    client = TestClient(main.app)
    response = client.get("/leads")

    assert response.status_code == 200
    assert "badge-signal" in response.text
    assert "Gone quiet: 90 days" in response.text


def test_leads_list_shows_cadence_fallback_when_no_signal(monkeypatch):
    fake_leads = [_fake_leads_list_row(
        activity={"kind": "cadence", "text": "3 releases", "detail": "last 5d ago"},
    )]
    monkeypatch.setattr(main, "fetch_leads", lambda: fake_leads)

    client = TestClient(main.app)
    response = client.get("/leads")

    assert response.status_code == 200
    assert "3 releases" in response.text
    assert "last 5d ago" in response.text


def test_leads_list_empty_state(monkeypatch):
    monkeypatch.setattr(main, "fetch_leads", list)

    client = TestClient(main.app)
    response = client.get("/leads")

    assert response.status_code == 200
    assert "NO LEADS ON FILE" in response.text


def test_lead_detail_renders_confidence_and_signal(monkeypatch):
    fake_lead = {
        "id": "lead-1",
        "company_name": "Acme Corp",
        "funding_round": "series-a",
        "amount_usd_fmt": "$10.0M",
        "sector": "Fintech",
        "investors": ["A Ventures"],
        "published_at": "2026-08-01T00:00:00Z",
        "distributor": "PR Newswire - Venture Capital",
        "fully_grounded": False,
        "grounding_failures": ["amount_usd: 10000000.0 has no matching dollar figure in source text"],
        "release": {"title": "Acme Corp Raises $10M", "url": "https://example.com/acme"},
        "switch_signal": {"score": 0.75, "reasons": ["Gone quiet: 90 days since last release"]},
        "ticker": "ACME",
        "investor_count": 1,
        "time_ago": "2d ago",
        "activity": {"kind": "signal", "text": "⚡ ACTIVITY", "detail": "Gone quiet: 90 days since last release"},
    }
    monkeypatch.setattr(main, "fetch_lead_detail", lambda lead_id: fake_lead)

    client = TestClient(main.app)
    response = client.get("/leads/lead-1")

    assert response.status_code == 200
    assert "UNVERIFIED FIELDS" in response.text
    assert "no matching dollar figure" in response.text
    assert "ACTIVITY SIGNAL" in response.text
    assert "Gone quiet: 90 days" in response.text
    assert "https://example.com/acme" in response.text


def test_lead_detail_404_when_not_found(monkeypatch):
    monkeypatch.setattr(main, "fetch_lead_detail", lambda lead_id: None)

    client = TestClient(main.app)
    response = client.get("/leads/does-not-exist")

    assert response.status_code == 404
