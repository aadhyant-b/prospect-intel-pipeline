"""FastAPI app: health check plus the funding-leads dashboard (read-only)."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from src.app.queries import compute_feed_stats, fetch_lead_detail, fetch_leads

app = FastAPI()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/leads")


@app.get("/leads")
def leads_list(request: Request):
    leads = fetch_leads()
    stats = compute_feed_stats(leads)
    return templates.TemplateResponse(request, "leads_list.html", {"leads": leads, "stats": stats})


@app.get("/leads/{lead_id}")
def lead_detail(request: Request, lead_id: str):
    lead = fetch_lead_detail(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return templates.TemplateResponse(request, "lead_detail.html", {"lead": lead})
