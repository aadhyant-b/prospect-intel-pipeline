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


def test_leads_list_renders_rows(monkeypatch):
    fake_leads = [{
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
    }]
    monkeypatch.setattr(main, "fetch_leads", lambda: fake_leads)

    client = TestClient(main.app)
    response = client.get("/leads")

    assert response.status_code == 200
    assert "Acme Corp" in response.text
    assert "$10.0M" in response.text
    assert "VERIFIED" in response.text
    assert "lead-1" in response.text


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
