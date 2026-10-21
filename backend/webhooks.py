"""PayPal webhook receiver. Events are stored in SQLite; payout events also update payout status."""
from __future__ import annotations

from fastapi import APIRouter, Request

from backend import db
from backend.paypal.webhooks import verify_signature

router = APIRouter()

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


def _apply_payout_event(etype: str, resource: dict):
    if etype.startswith("PAYMENT.PAYOUTSBATCH."):
        header = resource.get("batch_header") or {}
        if header.get("payout_batch_id"):
            db.update_batch_status(header["payout_batch_id"], header.get("batch_status"))
    elif etype.startswith("PAYMENT.PAYOUTS-ITEM."):
        sender_item_id = (resource.get("payout_item") or {}).get("sender_item_id")
        if resource.get("payout_batch_id") and sender_item_id:
            db.update_item(resource["payout_batch_id"], sender_item_id,
                           status=resource.get("transaction_status"),
                           payout_item_id=resource.get("payout_item_id"))


@router.post("/webhooks/paypal", tags=["webhooks"])
async def paypal_webhook(request: Request):
    event = await request.json()
    try:
        verified = verify_signature(dict(request.headers), event)
    except Exception as e:  # keep receiving even if verification is misconfigured
        print(f"[webhook] verification error: {e}")
        verified = False
    etype = event.get("event_type", "UNKNOWN")
    db.save_event(event, message=MESSAGES.get(etype, event.get("summary", "")), verified=verified)
    # Only let verified events change state; unverified ones are logged for inspection
    if verified is not False:
        _apply_payout_event(etype, event.get("resource") or {})
    print(f"[webhook] {etype} verified={verified}")
    return {"ok": True}


@router.get("/api/events", tags=["webhooks"])
def list_events():
    return db.list_events()
