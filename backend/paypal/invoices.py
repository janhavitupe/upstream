"""PayPal Invoicing v2: create, send, remind, search (Module 2).

Invoices are sent immediately with no approval step (by design); cancel() is the undo.
"""
from __future__ import annotations

from datetime import date, timedelta

from backend import config

from .client import paypal

OPEN_STATUSES = ("SENT", "UNPAID", "PARTIALLY_PAID")


def _invoice_id(resp: dict) -> str:
    return resp.get("id") or resp.get("href", "").rstrip("/").split("/")[-1]


def create_draft(recipient_email: str, items: list[dict], *, due_in_days: int = 14,
                 note: str | None = None, currency: str = "USD",
                 recipient_name: str | None = None) -> dict:
    """items: [{"name", "quantity", "unit_price", "description"?}]"""
    due = (date.today() + timedelta(days=max(0, due_in_days))).isoformat()
    billing = {"email_address": recipient_email}
    if recipient_name:
        billing["name"] = {"full_name": recipient_name}
    body = {
        "detail": {
            "currency_code": currency,
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due},
        },
        "invoicer": {"business_name": config.INVOICER_NAME},
        "primary_recipients": [{"billing_info": billing}],
        "items": [
            {
                "name": it["name"][:200],
                "description": it.get("description", "")[:1000] or None,
                "quantity": str(it.get("quantity", 1)),
                "unit_amount": {"currency_code": currency,
                                "value": f"{float(it['unit_price']):.2f}"},
            }
            for it in items
        ],
    }
    if note:
        body["detail"]["note"] = note[:4000]
    for it in body["items"]:
        if it["description"] is None:
            del it["description"]
    return paypal.request("POST", "/v2/invoicing/invoices", json=body)


def send(invoice_id: str, *, subject: str | None = None, note: str | None = None) -> dict:
    body: dict = {"send_to_invoicer": True}
    if subject:
        body["subject"] = subject
    if note:
        body["note"] = note
    return paypal.request("POST", f"/v2/invoicing/invoices/{invoice_id}/send", json=body)


def get(invoice_id: str) -> dict:
    return paypal.request("GET", f"/v2/invoicing/invoices/{invoice_id}")


def create_and_send(recipient_email: str, items: list[dict], **kwargs) -> dict:
    draft = create_draft(recipient_email, items, **kwargs)
    inv_id = _invoice_id(draft)
    send(inv_id)
    return get(inv_id)


def remind(invoice_id: str, note: str, subject: str | None = None) -> dict:
    body = {
        "subject": subject or "Friendly reminder about your invoice",
        "note": note[:4000],
        "send_to_invoicer": False,
    }
    return paypal.request("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", json=body)


def cancel(invoice_id: str, note: str = "This invoice was cancelled.") -> dict:
    body = {"subject": "Invoice cancelled", "note": note,
            "send_to_invoicer": True, "send_to_recipient": True}
    return paypal.request("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel", json=body)


def search(statuses=OPEN_STATUSES, page_size: int = 50) -> list[dict]:
    resp = paypal.request("POST", "/v2/invoicing/search-invoices",
                          params={"page": 1, "page_size": page_size, "total_required": "true"},
                          json={"status": list(statuses)})
    return resp.get("items", [])


def simplify(inv: dict) -> dict:
    detail = inv.get("detail", {})
    due = detail.get("payment_term", {}).get("due_date")
    recipients = inv.get("primary_recipients") or [{}]
    amount = inv.get("amount", {})
    days_overdue = 0
    if due:
        try:
            days_overdue = max(0, (date.today() - date.fromisoformat(due)).days)
        except ValueError:
            pass
    return {
        "id": inv.get("id"),
        "number": detail.get("invoice_number"),
        "recipient": recipients[0].get("billing_info", {}).get("email_address"),
        "amount": amount.get("value"),
        "currency": amount.get("currency_code") or detail.get("currency_code"),
        "status": inv.get("status"),
        "due_date": due,
        "days_overdue": days_overdue,
    }


def list_open() -> list[dict]:
    return [simplify(i) for i in search()]
