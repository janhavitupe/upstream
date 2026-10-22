"""API routes for Module 2 (Get paid): the billing chat, tracked invoices, clients."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import db
from backend.agent import billing
from backend.paypal import invoices as pp_invoices

router = APIRouter(prefix="/api/getpaid", tags=["2 · Get paid"])


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000,
                         examples=["Bill Acme $500 for the API integration, due in 14 days"])
    history: list[ChatTurn] = Field(default_factory=list)
    pending: dict | None = Field(None, description="Echo back the 'pending' from the last reply.")


@router.post("/chat")
def chat(body: ChatIn):
    return billing.run([t.model_dump() for t in body.history], body.message, body.pending)


@router.get("/invoices")
def tracked_invoices():
    return db.list_invoices()


@router.post("/invoices/{invoice_id}/cancel")
def cancel(invoice_id: str):
    """Undo: cancel an invoice Upstream sent."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    if inv["status"] not in billing.OPEN:
        raise HTTPException(409, f"This invoice is already {inv['status'].lower()}.")
    pp_invoices.cancel(invoice_id)
    db.update_invoice(invoice_id, status="CANCELLED")
    return db.get_invoice(invoice_id)


@router.get("/clients")
def clients():
    return {"clients": db.list_clients(),
            "chasing_paused": bool(db.get_setting("chasing_paused", False))}
