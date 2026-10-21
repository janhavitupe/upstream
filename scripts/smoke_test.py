"""
Upstream: PayPal sandbox smoke test.
Checks that OAuth, Payouts, Invoicing, Subscriptions (Catalog) and Orders all work.

Setup:
    pip install requests python-dotenv
    Create a .env file in the project root (copy .env.example):
        PAYPAL_CLIENT_ID=...
        PAYPAL_CLIENT_SECRET=...
        PAYPAL_BASE=https://api-m.sandbox.paypal.com
Run:
    python scripts/smoke_test.py
"""
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")

CLIENT = "acme-client@personal.example.com"
MAINTAINERS = [
    "maintainer-a@personal.example.com",
    "maintainer-b@personal.example.com",
    "maintainer-c@personal.example.com",
]
HOSPITAL = "city-hospital@business.example.com"


def get_token():
    r = requests.post(
        f"{BASE}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def report(name, r, ok_codes=(200, 201, 202, 204)):
    ok = r.status_code in ok_codes
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: HTTP {r.status_code}")
    if not ok:
        print("       ", r.text[:500])
    return ok


def test_payouts(h):
    body = {
        "sender_batch_header": {
            "sender_batch_id": f"smoke-{uuid.uuid4().hex[:12]}",
            "email_subject": "Thanks for your open-source work!",
            "email_message": "Upstream smoke test payout.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": email,
                "amount": {"value": "5.00", "currency": "USD"},
                "note": "Upstream test",
                "sender_item_id": f"item-{i}",
            }
            for i, email in enumerate(MAINTAINERS)
        ],
    }
    r = requests.post(f"{BASE}/v1/payments/payouts", headers=h, json=body, timeout=30)
    if report("Payouts: create batch", r):
        batch_id = r.json()["batch_header"]["payout_batch_id"]
        r2 = requests.get(f"{BASE}/v1/payments/payouts/{batch_id}", headers=h, timeout=30)
        if report("Payouts: batch status", r2):
            print("        batch_status =", r2.json()["batch_header"]["batch_status"])


def test_invoicing(h):
    body = {
        "detail": {"currency_code": "USD", "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED",
                                            "due_date": (date.today() + timedelta(days=14)).isoformat()},
                   "note": "Upstream smoke test invoice"},
        "invoicer": {"name": {"given_name": "Upstream", "surname": "Studio"}},
        "primary_recipients": [{"billing_info": {"email_address": CLIENT}}],
        "items": [{
            "name": "API integration",
            "quantity": "1",
            "unit_amount": {"currency_code": "USD", "value": "500.00"},
        }],
    }
    r = requests.post(f"{BASE}/v2/invoicing/invoices", headers=h, json=body, timeout=30)
    if report("Invoicing: create draft", r):
        inv_id = r.json()["href"].rstrip("/").split("/")[-1]
        r2 = requests.post(f"{BASE}/v2/invoicing/invoices/{inv_id}/send",
                           headers=h, json={"send_to_invoicer": True}, timeout=30)
        report("Invoicing: send", r2)
        print("        invoice_id =", inv_id)


def test_subscriptions(h):
    r = requests.post(f"{BASE}/v1/catalogs/products", headers=h,
                      json={"name": "Monthly retainer", "type": "SERVICE"}, timeout=30)
    if report("Subscriptions: create product", r):
        product_id = r.json()["id"]
        plan = {
            "product_id": product_id,
            "name": "Retainer $300/month",
            "billing_cycles": [{
                "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                "tenure_type": "REGULAR",
                "sequence": 1,
                "total_cycles": 0,
                "pricing_scheme": {"fixed_price": {"value": "300", "currency_code": "USD"}},
            }],
            "payment_preferences": {"auto_bill_outstanding": True},
        }
        r2 = requests.post(f"{BASE}/v1/billing/plans", headers=h, json=plan, timeout=30)
        if report("Subscriptions: create plan", r2):
            print("        plan_id =", r2.json()["id"])


def test_orders(h):
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {"currency_code": "USD", "value": "20.00"},
            "payee": {"email_address": HOSPITAL},
            "description": "Upstream smoke test donation",
        }],
    }
    r = requests.post(f"{BASE}/v2/checkout/orders", headers=h, json=body, timeout=30)
    if report("Orders: create (payee = hospital)", r):
        approve = next(l["href"] for l in r.json()["links"] if l["rel"] in ("approve", "payer-action"))
        print("        donor approve link =", approve)


if __name__ == "__main__":
    token = get_token()
    print("[PASS] OAuth: token received")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Prefer": "return=representation"}
    test_payouts(headers)
    test_invoicing(headers)
    test_subscriptions(headers)
    test_orders(headers)
