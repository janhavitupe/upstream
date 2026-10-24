"""PayPal Payouts: pay a whole team in one batch (team payroll)."""
from __future__ import annotations

import uuid

from .client import paypal


def create_batch(items: list[dict], *,
                 subject: str = "You have a payment from Upstream",
                 message: str = "Sent to you through Upstream.",
                 sender_batch_id: str | None = None) -> dict:
    """items: [{"email", "amount", "note"?, "item_id"?, "currency"?}]. One currency per batch."""
    body = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id or f"upstream-{uuid.uuid4().hex[:16]}",
            "recipient_type": "EMAIL",
            "email_subject": subject,
            "email_message": message,
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": it["email"],
                "amount": {"value": f"{float(it['amount']):.2f}",
                           "currency": it.get("currency", "USD")},
                "note": (it.get("note") or "Thank you for your work.")[:4000],
                "sender_item_id": (it.get("item_id") or f"item-{i}")[:63],
            }
            for i, it in enumerate(items)
        ],
    }
    return paypal.request("POST", "/v1/payments/payouts", json=body)


def get_batch(batch_id: str) -> dict:
    return paypal.request("GET", f"/v1/payments/payouts/{batch_id}")


def get_item(item_id: str) -> dict:
    return paypal.request("GET", f"/v1/payments/payouts-item/{item_id}")


def cancel_unclaimed_item(item_id: str) -> dict:
    return paypal.request("POST", f"/v1/payments/payouts-item/{item_id}/cancel")
