"""Webhook signature verification via PayPal's verify endpoint."""
from __future__ import annotations

from backend import config

from .client import paypal


def verify_signature(headers: dict, event: dict) -> bool | None:
    """Returns True/False, or None when PAYPAL_WEBHOOK_ID isn't configured yet."""
    if not config.PAYPAL_WEBHOOK_ID:
        return None
    h = {k.lower(): v for k, v in headers.items()}
    body = {
        "auth_algo": h.get("paypal-auth-algo"),
        "cert_url": h.get("paypal-cert-url"),
        "transmission_id": h.get("paypal-transmission-id"),
        "transmission_sig": h.get("paypal-transmission-sig"),
        "transmission_time": h.get("paypal-transmission-time"),
        "webhook_id": config.PAYPAL_WEBHOOK_ID,
        "webhook_event": event,
    }
    resp = paypal.request("POST", "/v1/notifications/verify-webhook-signature", json=body)
    return resp.get("verification_status") == "SUCCESS"
