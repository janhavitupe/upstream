"""PayPal Orders v2: donations paid straight to the hospital (Module 4).

The hospital's merchant account is the payee, so Upstream never holds the money.
"""
from __future__ import annotations

from backend import config

from .client import paypal


def create_donation(amount: float, payee_email: str, description: str, *,
                    currency: str = "USD", custom_id: str | None = None) -> dict:
    unit: dict = {
        "amount": {"currency_code": currency, "value": f"{float(amount):.2f}"},
        "payee": {"email_address": payee_email},
        "description": description[:127],
    }
    if custom_id:
        unit["custom_id"] = custom_id[:127]
    body = {
        "intent": "CAPTURE",
        "purchase_units": [unit],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "Upstream Verified Causes",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": f"{config.APP_BASE_URL}/donation/return",
            "cancel_url": f"{config.APP_BASE_URL}/donation/cancel",
        }}},
    }
    return paypal.request("POST", "/v2/checkout/orders", json=body)


def capture(order_id: str) -> dict:
    return paypal.request("POST", f"/v2/checkout/orders/{order_id}/capture", json={})


def approve_link(order: dict) -> str | None:
    return next((l["href"] for l in order.get("links", [])
                 if l.get("rel") in ("payer-action", "approve")), None)
