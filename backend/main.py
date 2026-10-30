"""Upstream API + dashboard.

Run:  uvicorn backend.main:app --reload
Then: http://localhost:8000 (dashboard) and http://localhost:8000/docs (API explorer)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, db, retainers, scheduler
from backend.github import GitHubError
from backend.paypal import invoices, orders
from backend.paypal.client import PayPalError
from backend import causes_api
from backend.getpaid_api import router as getpaid_router
from backend.guide import router as guide_router
from backend.payroll_api import router as payroll_router
from backend.retainer_portal import router as portal_router
from backend.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(_):
    db.init()
    stop = scheduler.start() if config.PAYROLL_SCHEDULER else None
    yield
    if stop:
        stop.set()


app = FastAPI(
    title="Upstream",
    version="0.1.0",
    description="AI money agent for independent developers, built on PayPal. "
                "Money in runs on its own; money out waits for your yes.",
    lifespan=lifespan,
)
app.include_router(webhook_router)
app.include_router(payroll_router)
app.include_router(getpaid_router)
app.include_router(portal_router)
app.include_router(causes_api.router)
app.include_router(guide_router)
FRONTEND = config.ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.middleware("http")
async def always_fresh_frontend(request, call_next):
    """Browsers must re-check the page and its scripts on every load (a cheap 304 when nothing
    changed), so after a deploy nobody runs an old script against the new page."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(PayPalError)
async def paypal_error_handler(_, e: PayPalError):
    status = e.status if 400 <= e.status < 600 else 500
    return JSONResponse(status_code=status, content={"error": "paypal", "detail": e.body})


@app.exception_handler(GitHubError)
async def github_error_handler(_, e: GitHubError):
    return JSONResponse(status_code=502, content={"error": "github", "detail": str(e)})


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(FRONTEND / "index.html")


@app.post("/api/tick", tags=["meta"])
def tick():
    """Run everything that is due (payroll, invoice chasing). Safe to call publicly: each job
    only does what its rules allow and never acts twice. The daily GitHub Action calls this."""
    return scheduler.tick_all()


@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "paypal_configured": config.paypal_configured(),
        "paypal_base": config.PAYPAL_BASE,
        "database": "postgres" if db.using_postgres() else "sqlite (data/upstream.db: wiped on hosts without a disk)",
        "webhook_verification": bool(config.PAYPAL_WEBHOOK_ID),
        "github_token": bool(config.GITHUB_TOKEN),
        "llm": config.LLM_MODEL if config.llm_configured() else False,
        "payroll_min_edit_minutes": config.PAYROLL_MIN_EDIT_MINUTES,
        "demo_hospital": config.DEMO_HOSPITAL_EMAIL,
        "demo": {
            "team": config.DEMO_TEAM_EMAILS,
            "client": config.DEMO_CLIENT_EMAIL,
            "hospital": config.DEMO_HOSPITAL_EMAIL,
        },
    }


# ---------- Module 2: Get paid (invoices, sent without an approval step) ----------

class InvoiceItem(BaseModel):
    name: str
    quantity: float = 1
    unit_price: float = Field(..., gt=0)
    description: str | None = None


class InvoiceIn(BaseModel):
    client_email: str
    client_name: str | None = None
    items: list[InvoiceItem]
    due_in_days: int = Field(14, ge=0, le=365)
    note: str | None = None


@app.post("/api/invoices", tags=["2 · Invoices"])
def create_invoice(body: InvoiceIn):
    inv = invoices.create_and_send(
        body.client_email,
        [i.model_dump(exclude_none=True) for i in body.items],
        due_in_days=body.due_in_days, note=body.note, recipient_name=body.client_name,
    )
    return invoices.simplify(inv)


@app.get("/api/invoices/open", tags=["2 · Invoices"])
def open_invoices():
    return invoices.list_open()


class RemindIn(BaseModel):
    note: str
    subject: str | None = None


@app.post("/api/invoices/{invoice_id}/remind", tags=["2 · Invoices"])
def remind_invoice(invoice_id: str, body: RemindIn):
    invoices.remind(invoice_id, body.note, body.subject)
    return {"ok": True, "invoice_id": invoice_id}


@app.post("/api/invoices/{invoice_id}/cancel", tags=["2 · Invoices"])
def cancel_invoice(invoice_id: str):
    invoices.cancel(invoice_id)
    return {"ok": True, "invoice_id": invoice_id}


# ---------- Module 3: Fair retainers (routes live in getpaid_api / retainer_portal) ----------

@app.get("/retainer/return", include_in_schema=False, response_class=HTMLResponse)
def retainer_return(subscription_id: str = ""):
    r = db.get_retainer(subscription_id) if subscription_id else None
    if r:
        retainers.refresh(r["id"])
        return RedirectResponse(f"/r/{r['token']}")
    return _page("Retainer approved", "Thanks! You'll get a heads-up before every renewal.")


@app.get("/retainer/cancel", include_in_schema=False, response_class=HTMLResponse)
def retainer_cancel():
    return _page("Retainer not approved", "No subscription was created.")


# ---------- Module 4: Pay the bill, not the person (donations) ----------

class DonationIn(BaseModel):
    amount: float = Field(..., gt=0)
    hospital_email: str
    campaign: str = Field(..., examples=["Surgery bill for Sam"])


@app.post("/api/donations", tags=["4 · Verified causes"])
def create_donation(body: DonationIn):
    order = orders.create_donation(body.amount, body.hospital_email, body.campaign)
    return {"order_id": order.get("id"), "status": order.get("status"),
            "donor_approval_link": orders.approve_link(order)}


@app.get("/donation/return", include_in_schema=False, response_class=HTMLResponse)
def donation_return(token: str = ""):
    if not token:
        return _page("Missing order", "No order token was returned by PayPal.")
    try:
        campaign_page = causes_api.donation_return(token)  # a donation to a verified campaign
    except PayPalError as e:
        return _page("Capture failed", f"<pre>{e.body}</pre>")
    if campaign_page:
        return campaign_page
    try:
        cap = orders.capture(token)
    except PayPalError as e:
        return _page("Capture failed", f"<pre>{e.body}</pre>")
    unit = (cap.get("purchase_units") or [{}])[0]
    payee = (unit.get("payee") or {}).get("email_address", "the provider")
    return _page("Thank you", f"Your donation was paid directly to <b>{payee}</b>. "
                 f"Order <code>{cap.get('id')}</code> is {cap.get('status')}.")


@app.get("/donation/cancel", include_in_schema=False, response_class=HTMLResponse)
def donation_cancel():
    return _page("Donation cancelled", "No money was moved.")


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><body style='font:16px system-ui;max-width:560px;"
            f"margin:15vh auto;padding:0 16px'><h1>{title}</h1><p>{body}</p>"
            f"<p><a href='/'>Back to Upstream</a></p></body>")
