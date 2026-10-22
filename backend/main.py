"""Upstream API + dashboard.

Run:  uvicorn backend.main:app --reload
Then: http://localhost:8000 (dashboard) and http://localhost:8000/docs (API explorer)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import config, db, payroll, report, requeue
from backend.oss.analyzer import analyze_repo
from backend.oss.github import GitHubError
from backend.paypal import invoices, orders, payouts, subscriptions
from backend.paypal.client import PayPalError
from backend.payroll_api import router as payroll_router
from backend.webhooks import router as webhook_router


@asynccontextmanager
async def lifespan(_):
    db.init()
    stop = payroll.start_scheduler() if config.PAYROLL_SCHEDULER else None
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
FRONTEND = config.ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


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
        result = analyze_repo(body.repo, body.budget, body.max_deps)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _apply_carryover(result)
    result["revision"] = 1
    result["history"] = [{"revision": 1, "at": db.now(), "by": result["explained_by"],
                          "note": "Rule-based split" + (" with AI review"
                                                        if result["explained_by"] == "llm" else "")}]
    return {"analysis_id": db.save_analysis(result), **result}


def _apply_carryover(result: dict):
    """Add money re-queued from unclaimed payouts to the same packages in this analysis.

    It is only marked as used when this analysis is actually paid.
    """
    deps = {d["package"]: d for d in result["dependencies"] if d.get("signals")}
    applied = []
    for c in db.open_carryover():
        dep = deps.get(c["package"])
        if not dep:
            continue
        dep["amount"] = round(dep["amount"] + c["amount"], 2)
        dep["carryover"] = round(dep.get("carryover", 0) + c["amount"], 2)
        applied.append({"id": c["id"], "package": c["package"], "amount": c["amount"]})
    if applied:
        total = sum(d["amount"] for d in result["dependencies"])
        for d in result["dependencies"]:
            d["percent"] = round(100 * d["amount"] / total, 1) if total else 0.0
        result["dependencies"].sort(key=lambda r: (-r["amount"], r["package"]))
        result["carryover"] = applied
        result["total"] = round(total, 2)
        result["warnings"].append(
            f"Includes ${sum(a['amount'] for a in applied):.2f} re-queued from unclaimed "
            f"payouts: {', '.join(sorted({a['package'] for a in applied}))}.")


@app.get("/api/oss/analyses", tags=["1 · OSS payouts"])
def oss_analyses():
    return db.list_analyses()


@app.get("/api/oss/analyses/{analysis_id}", tags=["1 · OSS payouts"])
def oss_analysis(analysis_id: str):
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found.")
    return {**analysis, "payout": db.batch_for_analysis(analysis_id)}


class AllocationIn(BaseModel):
    revision: int = Field(..., description="The revision you are editing (from the analysis).")
    amounts: dict[str, float] = Field(..., examples=[{"jinja2": 30, "click": 5}],
                                      description="package -> new amount in USD. Packages "
                                                  "left out keep their current amount.")


@app.put("/api/oss/analyses/{analysis_id}/allocation", tags=["1 · OSS payouts"])
def oss_edit_allocation(analysis_id: str, body: AllocationIn):
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found.")
    if db.batch_for_analysis(analysis_id):
        raise HTTPException(409, "This analysis was already paid; run a new analysis.")
    if body.revision != analysis.get("revision", 1):
        raise HTTPException(409, f"Revision {body.revision} is out of date; the latest is "
                                 f"{analysis.get('revision', 1)}. Reload and try again.")
    deps = {d["package"]: d for d in analysis["dependencies"]}
    unknown = [p for p in body.amounts if p not in deps]
    if unknown:
        raise HTTPException(400, f"Not in this analysis: {', '.join(unknown)}")
    unfundable = [p for p, a in body.amounts.items() if a > 0 and not deps[p].get("signals")]
    if unfundable:
        raise HTTPException(400, f"No source repository found, so these can't be paid: "
                                 f"{', '.join(unfundable)}")
    if any(a < 0 for a in body.amounts.values()):
        raise HTTPException(400, "Amounts can't be negative.")
    new = {p: round(d["amount"], 2) for p, d in deps.items()}
    new.update({p: round(a, 2) for p, a in body.amounts.items()})
    total = round(sum(new.values()), 2)
    if total <= 0:
        raise HTTPException(400, "The total must be more than $0.")
    if total > config.OSS_MONTHLY_CAP:
        raise HTTPException(400, f"Total ${total:.2f} exceeds the monthly cap of "
                                 f"${config.OSS_MONTHLY_CAP:.2f} (OSS_MONTHLY_CAP).")

    changes = {p: [deps[p]["amount"], a] for p, a in new.items() if a != deps[p]["amount"]}
    demo = config.DEMO_MAINTAINER_EMAILS
    for i, (p, d) in enumerate(deps.items()):
        if p in changes:
            d["user_edited"] = True
        d["amount"] = new[p]
        d["percent"] = round(100 * new[p] / total, 1)
        if d["amount"] > 0 and not d.get("suggested_email") and demo:
            d["suggested_email"] = demo[i % len(demo)]
    analysis["dependencies"].sort(key=lambda r: (-r["amount"], r["package"]))
    analysis["total"] = total
    analysis["revision"] = analysis.get("revision", 1) + 1
    analysis.setdefault("history", []).append(
        {"revision": analysis["revision"], "at": db.now(), "by": "user", "changes": changes})
    db.update_analysis(analysis_id, analysis)
    return {**analysis, "payout": None}


class PayoutIn(BaseModel):
    analysis_id: str
    revision: int = Field(..., description="The revision you reviewed. Approval pays exactly "
                                           "this version.")
    approved: bool = False
    emails: dict[str, str] = Field(
        default_factory=dict,
        description="Optional package -> PayPal email overrides. Amounts always come from "
                    "the saved analysis.")


@app.post("/api/oss/payout", tags=["1 · OSS payouts"])
def oss_payout(body: PayoutIn):
    if not body.approved:
        raise HTTPException(400, "Payouts move money out, so they need explicit approval "
                                 "(approved: true).")
    analysis = db.get_analysis(body.analysis_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found. Run /api/oss/analyze first.")
    if body.revision != analysis.get("revision", 1):
        raise HTTPException(409, f"You approved revision {body.revision}, but the latest is "
                                 f"{analysis.get('revision', 1)}. Review it and approve again.")
    existing = db.batch_for_analysis(body.analysis_id)
    if existing:
        raise HTTPException(409, f"This analysis was already paid in batch "
                                 f"{existing['batch_id']}.")
    lines = [d for d in analysis["dependencies"] if d.get("amount", 0) > 0]
    if not lines:
        raise HTTPException(400, "Nothing to pay.")
    total = round(sum(d["amount"] for d in lines), 2)
    if total > config.OSS_MONTHLY_CAP:
        raise HTTPException(400, f"Total ${total:.2f} exceeds the monthly cap of "
                                 f"${config.OSS_MONTHLY_CAP:.2f} (OSS_MONTHLY_CAP).")
    items = [{"email": body.emails.get(d["package"]) or d.get("suggested_email", ""),
              "amount": d["amount"],
              "note": _payout_note(d, analysis),
              "item_id": d["package"][:63]} for d in lines]
    missing = [i["item_id"] for i in items if "@" not in i["email"]]
    if missing:
        raise HTTPException(400, f"Missing PayPal email for: {', '.join(missing)}")
    # Carry-over is used only if its package is still being paid, and only once
    paid = {d["package"] for d in lines}
    carry_ids = [c["id"] for c in analysis.get("carryover", []) if c["package"] in paid]
    if not db.claim_carryover(carry_ids, body.analysis_id):
        raise HTTPException(409, "Some re-queued money in this analysis was already paid by "
                                 "another payout. Run a new analysis.")
    # Deterministic id: PayPal rejects a reused sender_batch_id, so a double approval
    # can't pay twice even if two requests race past the check above
    sender_batch_id = f"upstream-{body.analysis_id}"
    try:
        resp = payouts.create_batch(items, sender_batch_id=sender_batch_id)
    except PayPalError:
        db.release_carryover(body.analysis_id)
        raise
    header = resp.get("batch_header", {})
    db.save_batch(header["payout_batch_id"], body.analysis_id, sender_batch_id,
                  header.get("batch_status"), items)
    return {"total": total, "batch": header}


def _payout_note(dep: dict, analysis: dict) -> str:
    who = (dep.get("intended_for") or {}).get("login")
    head = f"For @{who}'s work on {dep['package']}" if who else f"For your work on {dep['package']}"
    if who and analysis.get("sandbox"):
        head += " (sandbox stand-in)"
    return f"{head}, from {analysis['repo']} via Upstream. {dep.get('explanation') or ''}".strip()


@app.get("/api/oss/payout/{batch_id}", tags=["1 · OSS payouts"])
def oss_payout_status(batch_id: str):
    """Refreshes status from PayPal (the fallback when webhooks are slow or not set up)."""
    data = payouts.get_batch(batch_id)
    header = data.get("batch_header", {})
    if db.get_batch(batch_id):
        db.update_batch_status(batch_id, header.get("batch_status"))
        for i in data.get("items", []):
            sender_item_id = (i.get("payout_item") or {}).get("sender_item_id")
            if sender_item_id:
                db.update_item(batch_id, sender_item_id, status=i.get("transaction_status"),
                               payout_item_id=i.get("payout_item_id"))
        # Money coming back needs no approval: past the grace period, unclaimed items are
        # cancelled (PayPal returns the funds) and re-queued for that package's next payout
        requeue.requeue(batch_id, config.UNCLAIMED_GRACE_DAYS)
    stored = db.get_batch(batch_id)
    if stored:
        return {**stored, "grace_days": config.UNCLAIMED_GRACE_DAYS}
    # A batch created outside Upstream (e.g. by the smoke test): show PayPal's view only
    return {
        "batch_id": batch_id, "status": header.get("batch_status"),
        "items": [{
            "sender_item_id": (i.get("payout_item") or {}).get("sender_item_id"),
            "email": (i.get("payout_item") or {}).get("receiver"),
            "amount": ((i.get("payout_item") or {}).get("amount") or {}).get("value"),
            "status": i.get("transaction_status"),
            "payout_item_id": i.get("payout_item_id"),
        } for i in data.get("items", [])],
    }


@app.post("/api/oss/payout/{batch_id}/requeue", tags=["1 · OSS payouts"])
def oss_requeue_unclaimed(batch_id: str):
    """Cancel unclaimed items now (no grace period) and re-queue them for the next payout."""
    if not db.get_batch(batch_id):
        raise HTTPException(404, "Batch not found.")
    oss_payout_status(batch_id)  # refresh statuses (and pick up anything PayPal returned)
    requeued, failed = requeue.requeue(batch_id, 0)
    return {"requeued": requeued, "failed": failed, "batch": db.get_batch(batch_id)}


@app.get("/report/{analysis_id}", include_in_schema=False, response_class=HTMLResponse)
def giving_report(analysis_id: str):
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "Report not found.")
    batch = db.batch_for_analysis(analysis_id)
    return report.render(analysis, db.get_batch(batch["batch_id"]) if batch else None)


@app.get("/api/oss/carryover", tags=["1 · OSS payouts"])
def oss_carryover():
    return db.open_carryover()


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
