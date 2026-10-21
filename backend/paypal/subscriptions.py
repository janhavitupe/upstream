"""PayPal Catalog + Subscriptions: monthly retainers (Module 3)."""
from __future__ import annotations

from backend import config

from .client import paypal


def create_product(name: str, description: str | None = None) -> dict:
    body = {"name": name[:127], "type": "SERVICE"}
    if description:
        body["description"] = description[:256]
    return paypal.request("POST", "/v1/catalogs/products", json=body)


def create_plan(product_id: str, name: str, monthly_price: float, currency: str = "USD") -> dict:
    body = {
        "product_id": product_id,
        "name": name[:127],
        "billing_cycles": [{
            "frequency": {"interval_unit": "MONTH", "interval_count": 1},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,
            "pricing_scheme": {"fixed_price": {"value": f"{float(monthly_price):.2f}",
                                               "currency_code": currency}},
        }],
        "payment_preferences": {"auto_bill_outstanding": True, "payment_failure_threshold": 2},
    }
    return paypal.request("POST", "/v1/billing/plans", json=body)


def create_subscription(plan_id: str, subscriber_email: str | None = None) -> dict:
    body: dict = {
        "plan_id": plan_id,
        "application_context": {
            "brand_name": config.INVOICER_NAME,
            "user_action": "SUBSCRIBE_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": f"{config.APP_BASE_URL}/retainer/return",
            "cancel_url": f"{config.APP_BASE_URL}/retainer/cancel",
        },
    }
    if subscriber_email:
        body["subscriber"] = {"email_address": subscriber_email}
    return paypal.request("POST", "/v1/billing/subscriptions", json=body)


def get(sub_id: str) -> dict:
    return paypal.request("GET", f"/v1/billing/subscriptions/{sub_id}")


def suspend(sub_id: str, reason: str = "Paused at client's request") -> dict:
    return paypal.request("POST", f"/v1/billing/subscriptions/{sub_id}/suspend",
                          json={"reason": reason[:128]})


def activate(sub_id: str, reason: str = "Resumed") -> dict:
    return paypal.request("POST", f"/v1/billing/subscriptions/{sub_id}/activate",
                          json={"reason": reason[:128]})


def cancel(sub_id: str, reason: str = "Retainer ended") -> dict:
    return paypal.request("POST", f"/v1/billing/subscriptions/{sub_id}/cancel",
                          json={"reason": reason[:128]})


def approve_link(sub: dict) -> str | None:
    return next((l["href"] for l in sub.get("links", []) if l.get("rel") == "approve"), None)


def next_billing_time(sub: dict) -> str | None:
    return sub.get("billing_info", {}).get("next_billing_time")
