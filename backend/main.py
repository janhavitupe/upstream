"""Upstream API + dashboard.

Run:  uvicorn backend.main:app --reload
Then: http://localhost:8000 (dashboard) and http://localhost:8000/docs (API explorer)
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from backend import config
from backend.oss.analyzer import analyze_repo
from backend.oss.github import GitHubError
from backend.paypal import invoices, orders, payouts, subscriptions
from backend.paypal.client import PayPalError
from backend.webhooks import router as webhook_router

app = FastAPI(
    title="Upstream",
    version="0.1.0",
    description="AI money agent for independent developers, built on PayPal. "
                "Money in runs on its own; money out waits for your yes.",
)
app.include_router(webhook_router)


@app.exception_handler(PayPalError)
async def paypal_error_handler(_, e: PayPalError):
    status = e.status if 400 <= e.status < 600 else 500
    return JSONResponse(status_code=status, content={"error": "paypal", "detail": e.body})


@app.exception_handler(GitHubError)
async def github_error_handler(_, e: GitHubError):
    return JSONResponse(status_code=502, content={"error": "github", "detail": str(e)})


@app.get("/", include_in_schema=False)
def root():
    # No dashboard yet: the interactive API explorer is the UI for now
    return RedirectResponse("/docs")


@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "paypal_configured": config.paypal_configured(),
        "paypal_base": config.PAYPAL_BASE,
        "webhook_verification": bool(config.PAYPAL_WEBHOOK_ID),
        "github_token": bool(config.GITHUB_TOKEN),
        "llm": config.LLM_MODEL if config.llm_configured() else False,
        "oss_monthly_cap": config.OSS_MONTHLY_CAP,
        "demo": {
            "maintainers": config.DEMO_MAINTAINER_EMAILS,
            "client": config.DEMO_CLIENT_EMAIL,
            "hospital": config.DEMO_HOSPITAL_EMAIL,
        },
    }


# ---------- Module 1: Fund your upstream (OSS payouts) ----------

class AnalyzeIn(BaseModel):
    repo: str = Field(..., examples=["pallets/flask"])
    budget: float = Field(100, gt=0, le=100000)
    max_deps: int = Field(10, ge=1, le=25)


@app.post("/api/oss/analyze", tags=["1 · OSS payouts"])
def oss_analyze(body: AnalyzeIn):
    try:
        return analyze_repo(body.repo, body.budget, body.max_deps)
    except ValueError as e:
        raise HTTPException(400, str(e))


class PayoutLine(BaseModel):
    package: str
    email: str
    amount: float = Field(..., gt=0)
    note: str | None = None


class PayoutIn(BaseModel):
    lines: list[PayoutLine]
    approved: bool = False


@app.post("/api/oss/payout", tags=["1 · OSS payouts"])
def oss_payout(body: PayoutIn):
    if not body.approved:
        raise HTTPException(400, "Payouts move money out, so they need explicit approval "
                                 "(approved: true).")
    if not body.lines:
        raise HTTPException(400, "Nothing to pay.")
    total = round(sum(l.amount for l in body.lines), 2)
    if total > config.OSS_MONTHLY_CAP:
        raise HTTPException(400, f"Total ${total:.2f} exceeds the monthly cap of "
                                 f"${config.OSS_MONTHLY_CAP:.2f} (OSS_MONTHLY_CAP).")
    missing = [l.package for l in body.lines if "@" not in l.email]
    if missing:
        raise HTTPException(400, f"Missing PayPal email for: {', '.join(missing)}")
    items = [{"email": l.email, "amount": l.amount,
              "note": l.note or f"Thank you for maintaining {l.package}.",
              "item_id": f"{l.package}"[:63]} for l in body.lines]
    resp = payouts.create_batch(items)
    return {"total": total, "batch": resp.get("batch_header", resp)}


@app.get("/api/oss/payout/{batch_id}", tags=["1 · OSS payouts"])
def oss_payout_status(batch_id: str):
    data = payouts.get_batch(batch_id)
    return {
        "batch": data.get("batch_header", {}),
        "items": [{
            "receiver": (i.get("payout_item") or {}).get("receiver"),
            "amount": ((i.get("payout_item") or {}).get("amount") or {}).get("value"),
            "status": i.get("transaction_status"),
            "item_id": i.get("payout_item_id"),
        } for i in data.get("items", [])],
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


# ---------- Module 3: Fair retainers (subscriptions) ----------

class RetainerIn(BaseModel):
    client_email: str
    service_name: str = Field(..., examples=["Monthly dev retainer"])
    monthly_price: float = Field(..., gt=0)


@app.post("/api/retainers", tags=["3 · Retainers"])
def create_retainer(body: RetainerIn):
    product = subscriptions.create_product(body.service_name)
    plan = subscriptions.create_plan(product["id"], f"{body.service_name}: "
                                     f"${body.monthly_price:.2f}/month", body.monthly_price)
    sub = subscriptions.create_subscription(plan["id"], body.client_email)
    return {"subscription_id": sub.get("id"), "status": sub.get("status"),
            "plan_id": plan["id"], "client_approval_link": subscriptions.approve_link(sub)}


@app.get("/api/retainers/{sub_id}", tags=["3 · Retainers"])
def get_retainer(sub_id: str):
    sub = subscriptions.get(sub_id)
    return {"id": sub_id, "status": sub.get("status"),
            "next_billing_time": subscriptions.next_billing_time(sub)}


@app.post("/api/retainers/{sub_id}/pause", tags=["3 · Retainers"])
def pause_retainer(sub_id: str):
    subscriptions.suspend(sub_id)
    return {"ok": True}


@app.post("/api/retainers/{sub_id}/resume", tags=["3 · Retainers"])
def resume_retainer(sub_id: str):
    subscriptions.activate(sub_id)
    return {"ok": True}


@app.get("/retainer/return", include_in_schema=False, response_class=HTMLResponse)
def retainer_return(subscription_id: str = ""):
    return _page("Retainer approved", f"Subscription <code>{subscription_id}</code> is set up. "
                 "You'll get a heads-up before every renewal.")


@app.get("/retainer/cancel", include_in_schema=False, response_class=HTMLResponse)
def retainer_cancel():
    return _page("Retainer not approved", "No subscription was created.")


# ---------- Module 4: Pay the bill, not the person (donations) ----------

class DonationIn(BaseModel):
    amount: float = Field(..., gt=0)
    hospital_email: str
    campaign: str = Field(..., examples=["Surgery bill for a maintainer"])


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
