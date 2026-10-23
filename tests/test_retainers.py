"""Fair retainers: creation, approval and payments, heads-ups, pause rules, client page, routes."""
from datetime import date, timedelta

import pytest

from backend import config, db, retainers
from backend.agent import billing


def _create(fake_paypal, amount=300.0, start=None):
    return retainers.create(db.find_client("Acme"), "API maintenance", amount, start)


def _active(fake_paypal, renews_in=30, paid=300.0):
    r = _create(fake_paypal)
    nb = (date.today() + timedelta(days=renews_in)).isoformat()
    fake_paypal.client_approves(r["id"], next_billing=f"{nb}T10:00:00Z", paid=paid)
    retainers.refresh(r["id"])
    return db.get_retainer(r["id"])


# ---------- creation ----------

def test_create_makes_a_subscription_waiting_for_the_client(fake_paypal):
    r = _create(fake_paypal)
    assert r["status"] == "APPROVAL_PENDING" and r["approve_url"].startswith("https://www.sandbox.paypal.com/")
    assert r["client_url"].endswith(f"/r/{r['token']}") and len(r["token"]) >= 12
    assert fake_paypal.subs[r["id"]]["start_time"] is None


def test_create_with_a_future_start_date(fake_paypal):
    start = (date.today() + timedelta(days=9)).isoformat()
    r = _create(fake_paypal, start=start)
    assert fake_paypal.subs[r["id"]]["start_time"] == f"{start}T09:00:00Z" and r["next_billing"] == start


@pytest.mark.parametrize("amount,start,error", [(0, None, "between"), (99999, None, "between"),
                                                (10, "2020-01-01", "past"), (10, "soon", "YYYY-MM-DD")])
def test_create_validation(fake_paypal, amount, start, error):
    with pytest.raises(retainers.RetainerError, match=error):
        _create(fake_paypal, amount, start)
    assert not fake_paypal.subs


# ---------- approval and payments ----------

def test_approval_and_payment_are_picked_up_once(fake_paypal):
    db.set_setting("auto_give_back_percent", 2)
    r = _active(fake_paypal)
    assert r["status"] == "ACTIVE" and r["next_billing"] and r["last_payment"]
    retainers.refresh(r["id"])  # nothing new
    events = [e["event_type"] for e in db.list_events()]
    assert events.count("UPSTREAM.RETAINER.ACTIVATED") == 1 and events.count("UPSTREAM.RETAINER.PAYMENT") == 1
    assert [p["amount"] for p in db.open_pot()] == [6.0]
    fake_paypal.subs[r["id"]]["billing_info"]["last_payment"] = {"time": "2026-11-23T10:00:00Z",
                                                                 "amount": {"value": "300.00"}}
    retainers.refresh(r["id"])  # the next month's charge
    assert [e["event_type"] for e in db.list_events()].count("UPSTREAM.RETAINER.PAYMENT") == 2


# ---------- the heads-up ----------

def test_heads_up_comes_three_days_before_renewal_once_with_the_work(fake_paypal, demo_day):
    r = _active(fake_paypal, renews_in=30)
    db.add_work(r["id"], "Upgraded to Node 22")
    db.add_work(r["id"], "Shipped rate limiting")
    demo_day(26)
    assert retainers.heads_up_due() == []
    demo_day(27)
    assert retainers.heads_up_due() == ["heads-up Acme"]
    assert retainers.heads_up_due() == []
    note = db.get_retainer(r["id"])["heads_up_note"]
    assert "Node 22" in note and "rate limiting" in note and f"/r/{r['token']}" in note
    ev = next(e for e in db.list_events() if e["event_type"] == "UPSTREAM.RETAINER.HEADS_UP")
    assert "Ready to send" in ev["message"]


def test_next_heads_up_only_has_new_work(fake_paypal, demo_day):
    r = _active(fake_paypal, renews_in=30)
    db.add_work(r["id"], "October work")
    demo_day(27); retainers.heads_up_due()
    db.add_work(r["id"], "November work")
    nb = (date.today() + timedelta(days=60)).isoformat()
    db.update_retainer(r["id"], next_billing=nb)
    demo_day(57); retainers.heads_up_due()
    note = db.get_retainer(r["id"])["heads_up_note"]
    assert "November work" in note and "October work" not in note


def test_heads_up_with_ai_keeps_the_pause_link(fake_paypal, fake_llm, demo_day):
    r = _active(fake_paypal, renews_in=3)
    db.add_work(r["id"], "Fixed the webhooks")
    fake_llm.reply("Hi Acme! Your retainer renews soon. This month we fixed the webhooks. Thanks!")
    retainers.heads_up_due()
    note = db.get_retainer(r["id"])["heads_up_note"]
    assert note.startswith("Hi Acme!") and note.endswith(f"/r/{r['token']}")  # link appended
    assert "Fixed the webhooks" in fake_llm.prompt() and "No work log" not in fake_llm.prompt()


def test_heads_up_is_emailed_when_smtp_is_set(fake_paypal, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent.append(("tls",))
        def login(self, user, pw): sent.append(("login", user))
        def send_message(self, msg): sent.append(("send", msg["To"], msg["Subject"]))

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "me@example.com")
    r = _active(fake_paypal, renews_in=2)
    retainers.heads_up_due()
    assert ("send", "acme@example.com", f"Your API maintenance retainer renews on {r['next_billing']}") in sent
    ev = next(e for e in db.list_events() if e["event_type"] == "UPSTREAM.RETAINER.HEADS_UP")
    assert "Emailed to the client" in ev["message"]


# ---------- pause / resume / cancel ----------

def test_pause_resume_cancel_rules(fake_paypal):
    pending = _create(fake_paypal)
    with pytest.raises(retainers.RetainerError):
        retainers.pause(pending["id"], "you")
    r = _active(fake_paypal)
    assert retainers.pause(r["id"], "client")["status"] == "SUSPENDED"
    assert fake_paypal.subs[r["id"]]["status"] == "SUSPENDED"
    with pytest.raises(retainers.RetainerError):
        retainers.pause(r["id"], "you")
    assert retainers.resume(r["id"], "you")["status"] == "ACTIVE"
    assert retainers.cancel(r["id"])["status"] == "CANCELLED"
    with pytest.raises(retainers.RetainerError):
        retainers.cancel(r["id"])
    assert retainers.cancel(pending["id"])["status"] == "CANCELLED"


# ---------- the client's page ----------

def test_client_page_and_one_click_pause(client, fake_paypal):
    r = _active(fake_paypal)
    db.add_work(r["id"], "Shipped search")
    page = client.get(f"/r/{r['token']}").text
    assert "API maintenance" in page and "300.00" in page and "Shipped search" in page
    assert "Pause the next renewal" in page
    paused = client.post(f"/r/{r['token']}/pause").text
    assert "Paused." in paused and fake_paypal.subs[r["id"]]["status"] == "SUSPENDED"
    assert db.get_retainer(r["id"])["paused_by"] == "client"
    assert "Resumed" in client.post(f"/r/{r['token']}/resume").text
    assert client.get("/r/not-a-token").status_code == 404


def test_pending_client_page_offers_approval(client, fake_paypal):
    r = _create(fake_paypal)
    assert "Approve with PayPal" in client.get(f"/r/{r['token']}").text


def test_paypal_return_redirects_to_the_client_page(client, fake_paypal):
    r = _create(fake_paypal)
    fake_paypal.client_approves(r["id"])
    page = client.get(f"/retainer/return?subscription_id={r['id']}")
    assert page.status_code == 200 and "Active" in page.text
    assert "approved" in client.get("/retainer/return").text.lower()


# ---------- chat tools, routes, webhooks ----------

def test_retainer_chat_tools(fake_paypal):
    with pytest.raises(billing.UnknownClient):
        billing.create_retainer("Initech", "Support", 100)
    with pytest.raises(billing.ToolError, match="between"):
        billing.create_retainer("Acme", "Support", -1)
    r = billing.create_retainer("Acme", "Support", 100)
    assert billing.log_work("acme", "Fixed bugs")["note"] == "Fixed bugs"
    with pytest.raises(billing.ToolError, match="already logged"):
        billing.log_work("Acme", "fixed bugs")
    assert billing.list_retainers()[0]["monthly"] == 100
    with pytest.raises(billing.ToolError):
        billing.change_retainer("Acme", "pause")             # not active yet
    assert billing.change_retainer("Acme", "cancel")["status"] == "CANCELLED"
    with pytest.raises(billing.ToolError, match="No retainer"):
        billing.log_work("Acme", "more")
    billing.create_retainer("Acme", "Hosting", 20)
    billing.create_retainer("Acme", "Design", 50)
    with pytest.raises(billing.ToolError, match="More than one"):
        billing.log_work("Acme", "x")
    assert r["id"]


def test_retainer_routes(client, fake_paypal):
    r = _active(fake_paypal)
    rid = r["id"]
    assert client.post(f"/api/getpaid/retainers/{rid}/work", json={"note": "Shipped search"}).json()["work"][0]["note"] == "Shipped search"
    assert client.post(f"/api/getpaid/retainers/{rid}/work", json={"note": "x"}).status_code == 422
    listed = client.get("/api/getpaid/retainers").json()[0]
    assert listed["client_url"].endswith(r["token"]) and listed["next_heads_up"]
    assert client.post(f"/api/getpaid/retainers/{rid}/pause").json()["status"] == "SUSPENDED"
    assert client.post(f"/api/getpaid/retainers/{rid}/pause").status_code == 409
    assert client.post(f"/api/getpaid/retainers/{rid}/resume").json()["status"] == "ACTIVE"
    assert client.post(f"/api/getpaid/retainers/{rid}/refresh").status_code == 200
    assert client.post(f"/api/getpaid/retainers/{rid}/explode").status_code == 404
    assert client.post("/api/getpaid/retainers/nope/pause").status_code == 404
    assert client.post(f"/api/getpaid/retainers/{rid}/cancel").json()["status"] == "CANCELLED"


def test_subscription_webhooks_refresh_from_paypal(client, fake_paypal):
    r = _create(fake_paypal)
    fake_paypal.client_approves(r["id"], paid=300)
    client.post("/webhooks/paypal", json={"event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
                                          "resource": {"id": r["id"]}})
    assert db.get_retainer(r["id"])["status"] == "ACTIVE"
    fake_paypal.subs[r["id"]]["billing_info"]["last_payment"] = {"time": "2026-12-01T00:00:00Z",
                                                                 "amount": {"value": "300.00"}}
    client.post("/webhooks/paypal", json={"event_type": "PAYMENT.SALE.COMPLETED",
                                          "resource": {"billing_agreement_id": r["id"]}})
    assert db.get_retainer(r["id"])["last_payment"] == "2026-12-01T00:00:00Z"
