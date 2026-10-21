"""PayPal webhook receiver. Events are kept in memory and shown on the dashboard."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from backend.paypal.webhooks import verify_signature

router = APIRouter()
EVENTS: deque = deque(maxlen=200)

MESSAGES = {
    "PAYMENT.PAYOUTSBATCH.SUCCESS": "OSS payout batch completed",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": "A maintainer received their payout",
    "PAYMENT.PAYOUTS-ITEM.UNCLAIMED": "A payout is unclaimed (recipient has no PayPal account yet)",
    "PAYMENT.PAYOUTS-ITEM.RETURNED": "A payout was returned unclaimed",
    "INVOICING.INVOICE.PAID": "Invoice paid. Consider setting aside ~25% for tax.",
    "INVOICING.INVOICE.CANCELLED": "Invoice cancelled",
    "BILLING.SUBSCRIPTION.ACTIVATED": "Retainer activated by the client",
    "BILLING.SUBSCRIPTION.SUSPENDED": "Retainer paused",
    "BILLING.SUBSCRIPTION.CANCELLED": "Retainer cancelled",
    "PAYMENT.SALE.COMPLETED": "Retainer payment received",
    "PAYMENT.CAPTURE.COMPLETED": "Donation captured and paid to the provider",
}


@router.post("/webhooks/paypal", tags=["webhooks"])
async def paypal_webhook(request: Request):
    event = await request.json()
    try:
        verified = verify_signature(dict(request.headers), event)
    except Exception as e:  # keep receiving even if verification is misconfigured
        print(f"[webhook] verification error: {e}")
        verified = False
    etype = event.get("event_type", "UNKNOWN")
    EVENTS.appendleft({
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_type": etype,
        "resource_id": (event.get("resource") or {}).get("id"),
        "message": MESSAGES.get(etype, event.get("summary", "")),
        "verified": verified,
    })
    print(f"[webhook] {etype} verified={verified}")
    return {"ok": True}


@router.get("/api/events", tags=["webhooks"])
def list_events():
    return list(EVENTS)
