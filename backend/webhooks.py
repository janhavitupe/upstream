"""PayPal webhook receiver. Events are stored in SQLite; payout events also update payout status."""
from __future__ import annotations

from fastapi import APIRouter, Request

from backend import causes, chasing, config, db, retainers
from backend.paypal.webhooks import verify_signature

router = APIRouter()

MESSAGES = {
    "PAYMENT.PAYOUTSBATCH.SUCCESS": "Payroll batch completed",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": "A team member received their pay",
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


def _apply_invoice_event(etype: str, resource: dict):
    if not resource.get("id") or not db.get_invoice(resource["id"]):
        return  # not an invoice Upstream sent
    if etype == "INVOICING.INVOICE.PAID":
        chasing.mark_paid(resource["id"], resource.get("status") or "PAID")
    elif etype == "INVOICING.INVOICE.CANCELLED":
        db.update_invoice(resource["id"], status="CANCELLED")


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
    # Only verified events change state. Locally, with no PAYPAL_WEBHOOK_ID (verified is None),
    # unverified events are allowed for testing; on a public deploy anyone could send them.
    if verified or (verified is None and not config.IS_PUBLIC_DEPLOY):
        _apply_payout_event(etype, event.get("resource") or {})
        if etype.startswith("INVOICING.INVOICE."):
            _apply_invoice_event(etype, event.get("resource") or {})
        if etype == "PAYMENT.CAPTURE.COMPLETED":  # a donation captured (e.g. donor closed the tab)
            order_id = (((event.get("resource") or {}).get("supplementary_data") or {})
                        .get("related_ids") or {}).get("order_id")
            if order_id and db.get_donation(order_id):
                causes.complete_donation(order_id, capture=False)
        if etype.startswith("BILLING.SUBSCRIPTION.") or etype == "PAYMENT.SALE.COMPLETED":
            res = event.get("resource") or {}
            sub_id = res.get("billing_agreement_id") if etype == "PAYMENT.SALE.COMPLETED" else res.get("id")
            if sub_id and db.get_retainer(sub_id):
                retainers.refresh(sub_id)  # PayPal is the source of truth: re-read it
    print(f"[webhook] {etype} verified={verified}")
    return {"ok": True}


@router.get("/api/events", tags=["webhooks"])
def list_events():
    return db.list_events()
