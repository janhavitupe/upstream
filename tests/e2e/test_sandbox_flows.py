"""End-to-end: every money flow against the real PayPal sandbox.

Run:   UPSTREAM_E2E=1 python -m pytest -m e2e          (PowerShell: $env:UPSTREAM_E2E="1"; python -m pytest -m e2e)
Needs PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET and the sandbox DEMO_* emails (environment or .env).
Uses tiny sandbox amounts ($1-$5) and cleans up where PayPal allows. GitHub and AI calls are
replaced so these tests only depend on PayPal (the chat test uses the AI if a key is set).
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend import chasing, config, db, payroll, retainers
from backend.agent import billing
from backend.paypal import invoices as pp_invoices
from backend.paypal import payouts
from backend.paypal.client import paypal

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not config.paypal_configured(), reason="PayPal sandbox keys not set"),
    pytest.mark.skipif("sandbox" not in config.PAYPAL_BASE, reason="never against live PayPal"),
]


@pytest.fixture(autouse=True)
def no_ai_unless_asked(request, monkeypatch):
    """Reminder notes etc. use the templates, so these tests don't depend on the AI's limits."""
    if "uses_ai" not in request.keywords:
        monkeypatch.setattr(config, "LLM_API_KEY", "")


def _soon(days=7):
    return (date.today() + timedelta(days=days)).isoformat()


def _client_pays(invoice_id, value):
    """Act as the client: record a payment on the invoice in PayPal."""
    paypal.request("POST", f"/v2/invoicing/invoices/{invoice_id}/payments",
                   json={"method": "BANK_TRANSFER", "payment_date": date.today().isoformat(),
                         "amount": {"currency_code": "USD", "value": f"{value:.2f}"}})


def _cancel_quietly(invoice_id):
    try:
        pp_invoices.cancel(invoice_id)
    except Exception:
        pass  # already paid or cancelled


def test_invoice_paid_is_detected_and_stops_chasing():
    inv = billing.create_invoice("Acme", [{"name": "E2E check", "unit_price": 5}], due_date=_soon())
    assert inv["status"] == "SENT" and inv["payer_url"]
    _client_pays(inv["id"], 5)
    assert f"paid #{inv['number']}" in chasing.tick(force_refresh=True)
    assert db.get_invoice(inv["id"])["status"] in chasing.PAID
    assert any("Payment landed" in e["message"] for e in db.list_events())
    db.set_setting("demo_offset_days", 40)
    assert not [a for a in chasing.chase_due() if inv["number"] in a]


def test_overdue_invoice_gets_one_real_reminder():
    inv = billing.create_invoice("Acme", [{"name": "E2E reminder", "unit_price": 3}], due_date=_soon(1))
    try:
        db.set_setting("demo_offset_days", 2)
        assert chasing.chase_due() == [f"friendly reminder #{inv['number']}"]
        assert chasing.chase_due() == []
        db.set_setting("demo_offset_days", 30)              # jumps straight to the final notice
        assert chasing.chase_due() == [f"final reminder #{inv['number']}"]
    finally:
        _cancel_quietly(inv["id"])


def test_pay_in_parts_and_partial_payment():
    inv = billing.create_invoice("Acme", [{"name": "E2E partial", "unit_price": 10}], due_date=_soon())
    from backend.getpaid_api import PartialIn, allow_partial
    allow_partial(inv["id"], PartialIn(minimum=1))
    _client_pays(inv["id"], 4)
    assert f"partial payment #{inv['number']}" in chasing.tick(force_refresh=True)
    row = db.get_invoice(inv["id"])
    assert row["status"] == "PARTIALLY_PAID" and chasing.owed(row) == 6


def test_write_off_cancels_in_paypal():
    inv = billing.create_invoice("Acme", [{"name": "E2E write-off", "unit_price": 2}], due_date=_soon())
    from backend.getpaid_api import write_off
    assert write_off(inv["id"])["status"] == "CANCELLED"
    assert pp_invoices.get(inv["id"])["status"] == "CANCELLED"


def test_payroll_pays_itself_on_payday(client, monkeypatch):
    assert config.DEMO_TEAM_EMAILS, "DEMO_TEAM_EMAILS must list sandbox accounts"
    email = config.DEMO_TEAM_EMAILS[0]

    def split(repo, owner, total, top_n, since, until, emails=None):  # skip GitHub, keep PayPal
        return {"repo": repo, "owner_login": owner, "window_start": payroll._iso(since),
                "window_end": payroll._iso(until), "total": total, "sandbox": True, "warnings": [],
                "contributors": [{"login": "e2e-dev", "amount": total, "percent": 100.0,
                                  "commits": 1, "email": email, "explanation": "E2E"}]}
    monkeypatch.setattr(payroll, "compute_split", split)
    sid = db.create_schedule({"repo": "e2e/check", "owner_login": "me", "total": 1, "pay_day": 25,
                              "top_n": 1, "notice_days": 2})
    db.update_schedule(sid, created_at="2026-10-01T00:00:00+00:00")
    t = lambda d: datetime(2026, 10, d, 12, tzinfo=timezone.utc)  # noqa: E731
    assert payroll.tick(t(23)) == ["preview e2e/check 2026-10"]
    assert payroll.tick(t(26)) == ["paid e2e/check 2026-10"]
    assert payroll.tick(t(27)) == []
    run = db.runs_for_schedule(sid)[0]
    assert payouts.get_batch(run["batch_id"])["batch_header"]["payout_batch_id"] == run["batch_id"]
    status = client.get(f"/api/payroll/batches/{run['batch_id']}").json()
    assert len(status["items"]) == 1 and status["items"][0]["payout_item_id"]


def test_retainer_subscription_is_created_with_approval_link():
    r = retainers.create(db.find_client("Acme"), "E2E retainer", 1.0)
    try:
        assert r["status"] == "APPROVAL_PENDING"
        assert r["approve_url"].startswith("https://www.sandbox.paypal.com/")
    finally:
        retainers.cancel(r["id"])


def test_donation_order_pays_the_hospital(client):
    r = client.post("/api/donations", json={"amount": 2, "hospital_email": config.DEMO_HOSPITAL_EMAIL,
                                            "campaign": "E2E check"}).json()
    assert r["order_id"] and "sandbox.paypal.com" in r["donor_approval_link"]


@pytest.mark.uses_ai
@pytest.mark.skipif(not config.llm_configured(), reason="LLM_API_KEY not set")
def test_chat_creates_a_real_invoice():
    out = billing.run([], f"Bill Acme $2 for an end-to-end check, due {_soon(10)}")
    sent = [a["data"] for a in out["actions"] if a["type"] == "invoice_sent"]
    assert sent, out["reply"]
    try:
        assert sent[0]["amount"] == 2 and sent[0]["client_name"] == "Acme"
    finally:
        _cancel_quietly(sent[0]["id"])


@pytest.mark.uses_ai
@pytest.mark.skipif(not config.llm_configured(), reason="LLM_API_KEY not set")
def test_verified_cause_with_a_real_bill_and_donation_order(client, tmp_path, monkeypatch):
    """The vision model reads a generated bill, code verifies it, and a donation order is
    created in PayPal with the hospital as the payee."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    from backend import causes
    monkeypatch.setattr(config, "BILLS_DIR", tmp_path)
    try:
        f, big = ImageFont.truetype("arial.ttf", 28), ImageFont.truetype("arialbd.ttf", 40)
    except OSError:
        f = big = ImageFont.load_default()
    img = Image.new("RGB", (1000, 800), "white")
    d = ImageDraw.Draw(img)
    when = (date.today() - timedelta(days=10)).isoformat()
    d.text((50, 50), "CITY GENERAL HOSPITAL", font=big, fill="black")
    for i, line in enumerate([f"Patient: Sam Lee", f"Statement date: {when}", "Appendectomy   $2,800.00",
                              "Pharmacy          $200.00", "AMOUNT DUE   $3,000.00"]):
        d.text((50, 150 + 60 * i), line, font=f, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    c = causes.create("E2E cause", "End-to-end check", "Sam Lee", "City General Hospital",
                      config.DEMO_HOSPITAL_EMAIL, 3000, buf.getvalue(), "image/png")
    assert c["verdict"] == "verified", c["checks"]
    link = causes.start_donation(c["token"], 2)
    assert "sandbox.paypal.com" in link
