"""Get paid: the billing chat, every invoice route, payment detection, your style."""
from datetime import date, timedelta

import pytest

from backend import chasing, db
from backend.agent import billing
from backend.paypal.client import PayPalError


def _soon(days=14):
    return (date.today() + timedelta(days=days)).isoformat()


def _bill(fake_paypal, client="Acme", amount=300, due=None, term=30):
    """term: pretend it was sent this many days before it's due (short terms chase urgently)."""
    inv = billing.create_invoice(client, [{"name": "Work", "unit_price": amount}],
                                 due_date=due or _soon())
    sent = date.fromisoformat(inv["due_date"]) - timedelta(days=term)
    db.update_invoice(inv["id"], created_at=f"{sent.isoformat()}T09:00:00+00:00")
    return inv


# ---------- chat tools ----------

def test_create_invoice_sends_it_and_saves_it(fake_paypal):
    inv = billing.create_invoice("acme", [{"name": "API", "unit_price": 500},
                                          {"name": "Support", "quantity": 2, "unit_price": 80}],
                                 due_date=_soon(10))
    assert inv["amount"] == 660 and inv["client_name"] == "Acme" and inv["status"] == "SENT"
    assert inv["payer_url"] and inv["due_date"] == _soon(10)
    assert fake_paypal.invoices[inv["id"]]["status"] == "SENT"
    assert db.get_invoice(inv["id"])["amount"] == 660


@pytest.mark.parametrize("kwargs,error", [
    ({"client_name": "Initech", "items": [{"name": "x", "unit_price": 1}]}, "Unknown client"),
    ({"client_name": "Acme", "items": []}, "at least one item"),
    ({"client_name": "Acme", "items": [{"name": "x", "unit_price": -5}]}, "Invalid line item"),
    ({"client_name": "Acme", "items": [{"name": "x", "unit_price": 99999}]}, "limit"),
    ({"client_name": "Acme", "items": [{"name": "x", "unit_price": 5}], "due_date": "2020-01-01"}, "past"),
    ({"client_name": "Acme", "items": [{"name": "x", "unit_price": 5}], "due_date": "next week"}, "YYYY-MM-DD"),
])
def test_create_invoice_validation(fake_paypal, kwargs, error):
    with pytest.raises(billing.ToolError, match=error):
        billing.create_invoice(**kwargs)
    assert not fake_paypal.invoices


def test_add_list_cancel_and_chasing_tools(fake_paypal):
    with pytest.raises(billing.ToolError):
        billing.add_client("Initech", "not-an-email")
    billing.add_client("Initech", "initech@example.com")
    a = _bill(fake_paypal, "Acme", 100)
    i = _bill(fake_paypal, "Initech", 50)
    assert {r["client"] for r in billing.list_invoices("open")} == {"Acme", "Initech"}
    assert billing.list_invoices("overdue") == []
    assert billing.cancel_invoice(client_name="Initech")["status"] == "CANCELLED"
    assert fake_paypal.invoices[i["id"]]["status"] == "CANCELLED"
    with pytest.raises(billing.ToolError, match="No matching"):
        billing.cancel_invoice(client_name="Initech")
    _bill(fake_paypal, "Acme", 200)
    with pytest.raises(billing.ToolError, match="More than one"):
        billing.cancel_invoice(client_name="Acme")
    assert billing.cancel_invoice(invoice_number=a["number"])["status"] == "CANCELLED"
    assert billing.set_chasing(False, "Acme") == {"client": "Acme", "chasing": False}
    assert billing.set_chasing(False) == {"everyone": True, "chasing": False}
    assert db.get_setting("chasing_paused") is True


# ---------- the agent loop ----------

def test_chat_runs_tools_and_returns_cards(fake_paypal, fake_llm):
    fake_llm.tool("create_invoice", {"client_name": "Acme", "items": [{"name": "API", "unit_price": 500}],
                                     "due_date": _soon(7)}).reply("Invoice 0001 sent.")
    out = billing.run([], "Bill Acme $500 for the API, due next week")
    assert out["reply"] == "Invoice 0001 sent."
    assert out["actions"][0]["type"] == "invoice_sent" and out["actions"][0]["data"]["amount"] == 500
    assert "Calendar" in fake_llm.prompt(0) and _soon(7) in fake_llm.prompt(0)
    tool_result = fake_llm.requests[1]["messages"][-1]
    assert tool_result["role"] == "tool" and '"ok": true' in tool_result["content"]


def test_unknown_client_then_email_finishes_the_invoice(fake_paypal, fake_llm):
    fake_llm.tool("create_invoice", {"client_name": "Initech", "items": [{"name": "Logo", "unit_price": 300}]})
    fake_llm.reply("What is Initech's email address?")
    first = billing.run([], "Bill Initech $300 for a logo")
    assert first["pending"] == {"client_name": "Initech", "request": "Bill Initech $300 for a logo"}
    # The model "forgets" to call add_client; the code saves the email anyway
    fake_llm.tool("create_invoice", {"client_name": "Initech", "items": [{"name": "Logo", "unit_price": 300}]})
    fake_llm.reply("Invoice sent to Initech.")
    second = billing.run([], "initech@example.com", first["pending"])
    assert [a["type"] for a in second["actions"]] == ["client_saved", "invoice_sent"]
    assert db.find_client("Initech")["email"] == "initech@example.com" and second["pending"] is None
    assert "complete the user's earlier request" in fake_llm.prompt(-2)


def test_tool_errors_are_given_back_to_the_model(fake_paypal, fake_llm):
    fake_llm.tool("cancel_invoice", {"client_name": "Nobody"}).reply("There's no such invoice.")
    out = billing.run([], "cancel nobody's invoice")
    assert out["actions"] == [] and '"ok": false' in fake_llm.requests[1]["messages"][-1]["content"]


def test_ai_failure_midway_says_exactly_what_happened(fake_paypal, fake_llm):
    from backend.agent.llm import LLMError
    fake_llm.tool("create_invoice", {"client_name": "Acme", "items": [{"name": "x", "unit_price": 5}]})
    calls = {"n": 0}

    def responder(body):
        calls["n"] += 1
        if calls["n"] == 1:
            return fake_llm.queue.pop(0)
        raise LLMError("rate limit")
    fake_llm.responder = responder
    out = billing.run([], "bill acme $5")
    assert "finish 1 step" in out["reply"] and out["actions"][0]["type"] == "invoice_sent"
    fake_llm.responder = lambda body: (_ for _ in ()).throw(LLMError("down"))
    assert "Nothing was sent" in billing.run([], "bill acme $5")["reply"]


def test_replies_are_cleaned():
    assert billing._dedupe(billing._clean("Sent to Acme (​ ).Sent to Acme.")) == "Sent to Acme."
    assert billing._dedupe("Sent. Due Nov 5.") == "Sent. Due Nov 5."


def test_chat_route(client, fake_paypal, fake_llm):
    fake_llm.reply("Hi! Who should I bill?")
    r = client.post("/api/getpaid/chat", json={"message": "hello", "history": [
        {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]})
    assert r.status_code == 200 and r.json()["reply"] == "Hi! Who should I bill?"
    assert client.post("/api/getpaid/chat", json={"message": ""}).status_code == 422


# ---------- invoice routes ----------

def test_invoice_view_and_undo(client, fake_paypal, demo_day):
    inv = _bill(fake_paypal, due=_soon(1))
    demo_day(3)
    row = client.get("/api/getpaid/invoices").json()[0]
    assert row["days_overdue"] == 2 and row["chasing"] == "on"
    assert row["next_reminder"]["tone"] == "friendly" and row["owed"] == 300
    db.set_client_chase("Acme", True)
    assert client.get("/api/getpaid/invoices").json()[0]["chasing"] == "off for this client"
    assert client.post(f"/api/getpaid/invoices/{inv['id']}/cancel").json()["status"] == "CANCELLED"
    assert client.post(f"/api/getpaid/invoices/{inv['id']}/cancel").status_code == 409
    assert client.post("/api/getpaid/invoices/nope/cancel").status_code == 404


def test_state_switches_and_demo_clock(client, fake_paypal, monkeypatch):
    st = client.post("/api/getpaid/chasing", json={"enabled": False}).json()
    assert st["chasing_paused"] is True and st["sandbox"] is True
    assert client.post("/api/getpaid/clients/Acme/chasing", json={"enabled": False}).json()["clients"][0]["dont_chase"]
    assert client.post("/api/getpaid/clients/Nobody/chasing", json={"enabled": False}).status_code == 404
    st = client.post("/api/getpaid/demo-clock", json={"days": 5}).json()
    assert st["demo_offset_days"] == 5 and st["today"] == (date.today() + timedelta(days=5)).isoformat()
    assert client.post("/api/getpaid/demo-clock", json={"days": 99}).status_code == 422
    from backend import config
    monkeypatch.setattr(config, "PAYPAL_BASE", "https://api-m.paypal.com")
    assert client.post("/api/getpaid/demo-clock", json={"days": 1}).status_code == 400


def test_check_detects_payments_partial_payments_and_opens(client, fake_paypal):
    full = _bill(fake_paypal, amount=100)
    part = _bill(fake_paypal, amount=300)
    fake_paypal.client_pays(full["id"])
    fake_paypal.client_pays(part["id"], 100)
    fake_paypal.client_opens(part["id"])
    st = client.post("/api/getpaid/check").json()
    assert f"paid #{full['number']}" in st["actions"] and f"partial payment #{part['number']}" in st["actions"]
    row = db.get_invoice(part["id"])
    assert row["status"] == "PARTIALLY_PAID" and chasing.owed(row) == 200 and row["viewed"] == 1
    messages = [e["message"] for e in st["log"]]
    assert any("paid $100.00 of" in m and "$200.00 is still owed" in m for m in messages)
    assert client.post("/api/getpaid/check").json()["actions"] == []  # noted once


def test_invoice_webhooks(client, fake_paypal):
    a = _bill(fake_paypal)
    b = _bill(fake_paypal)
    client.post("/webhooks/paypal", json={"event_type": "INVOICING.INVOICE.PAID",
                                          "resource": {"id": a["id"], "status": "PAID"}})
    client.post("/webhooks/paypal", json={"event_type": "INVOICING.INVOICE.CANCELLED",
                                          "resource": {"id": b["id"]}})
    client.post("/webhooks/paypal", json={"event_type": "INVOICING.INVOICE.PAID",
                                          "resource": {"id": "not-ours"}})
    assert db.get_invoice(a["id"])["status"] == "PAID" and db.get_invoice(b["id"])["status"] == "CANCELLED"


# ---------- needs you ----------

def _overdue_needing_you(fake_paypal, demo_day):
    inv = _bill(fake_paypal, due=_soon(0))
    demo_day(22); chasing.chase_due()
    demo_day(29); chasing.chase_due()
    return inv


def test_needs_you_view_trail_and_draft(client, fake_paypal, fake_llm, demo_day):
    def responder(body):
        text = body["messages"][-1]["content"]
        if "Draft a short personal message" in text:
            return {"content": "Hi Acme, is everything OK? Could we split it into two parts? "
                               "Thanks, Upstream Studio"}
        return {"content": "Hi Acme, a reminder that this invoice is overdue. Thank you!"}
    fake_llm.responder = responder
    inv = _overdue_needing_you(fake_paypal, demo_day)
    row = client.get("/api/getpaid/invoices").json()[0]
    assert row["needs_you"] and row["chase_stage"] == 2
    trail = client.get(f"/api/getpaid/invoices/{inv['id']}/trail").json()
    types = [e["type"] for e in trail["events"]]
    assert "UPSTREAM.CHASE.FINAL" in types and "UPSTREAM.INVOICE.NEEDS_YOU" in types
    assert all(e["note"] for e in trail["events"] if e["type"].startswith("UPSTREAM.CHASE."))
    client.post("/api/getpaid/check")  # learns from PayPal that the client never opened it
    d = client.post(f"/api/getpaid/invoices/{inv['id']}/draft").json()
    assert d["to"] == "acme@example.com" and "split" in d["draft"]
    assert "Never use placeholders" in fake_llm.prompt() and "never opened" in fake_llm.prompt()


def test_allow_partial_and_write_off(client, fake_paypal, demo_day):
    inv = _overdue_needing_you(fake_paypal, demo_day)
    assert client.post(f"/api/getpaid/invoices/{inv['id']}/allow-partial", json={"minimum": 300}).status_code == 400
    r = client.post(f"/api/getpaid/invoices/{inv['id']}/allow-partial", json={"minimum": 50}).json()
    assert r["partial_allowed"] == 1
    assert fake_paypal.invoices[inv["id"]]["configuration"]["partial_payment"]["minimum_amount_due"]["value"] == "50.00"
    fake_paypal.client_pays(inv["id"], 100)
    client.post("/api/getpaid/check")
    w = client.post(f"/api/getpaid/invoices/{inv['id']}/write-off").json()   # partly paid: local only
    assert w["status"] == "WRITTEN_OFF" and w["written_off"] == 1
    assert fake_paypal.invoices[inv["id"]]["status"] == "PARTIALLY_PAID"
    other = _bill(fake_paypal)
    assert client.post(f"/api/getpaid/invoices/{other['id']}/write-off").json()["status"] == "CANCELLED"
    assert client.post(f"/api/getpaid/invoices/{other['id']}/write-off").status_code == 409


# ---------- your writing style ----------

def test_voice_save_preview_and_use(client, fake_paypal, fake_llm):
    assert client.put("/api/getpaid/voice", json={"samples": ["hi"]}).status_code == 400
    samples = ["hey sam! shipped it this morning, have a look 🙌 — Jan"] * 2
    assert len(client.put("/api/getpaid/voice", json={"samples": samples}).json()["samples"]) == 2
    fake_llm.reply("Dear Acme, invoice #0042 is overdue. Regards").reply("hey acme! #0042 is overdue 🙌 — Jan")
    p = client.post("/api/getpaid/voice/preview", json={"tone": "final"}).json()
    assert p["generic"].startswith("Dear") and p["yours"].startswith("hey")
    assert "imitating these real messages" in fake_llm.prompt(-1)
    assert "imitating" not in fake_llm.prompt(-2)                    # the generic one has no style
    assert "tone level always wins" in fake_llm.prompt(-1)
    assert client.post("/api/getpaid/voice/preview", json={"tone": "angry"}).status_code == 422


def test_reminders_use_the_ai_note_and_fall_back_when_it_is_bad(fake_paypal, fake_llm, demo_day):
    inv = _bill(fake_paypal, due=_soon(0))
    fake_llm.reply("Hi Acme, a friendly reminder that invoice is now overdue. Thanks!")
    demo_day(2); chasing.chase_due()
    assert fake_paypal.reminders[-1][1]["note"].startswith("Hi Acme, a friendly")
    fake_llm.reply("ok")                                               # too short: template instead
    demo_day(8); chasing.chase_due()
    assert "$300.00" in fake_paypal.reminders[-1][1]["note"]
    assert db.get_invoice(inv["id"])["chase_stage"] == 2
    # the note is also on the invoice, so it shows in the reminder email; not re-sent for it
    assert fake_paypal.invoices[inv["id"]]["detail"]["note"] == fake_paypal.reminders[-1][1]["note"]
    assert fake_paypal.updates[-1][1]["send_to_recipient"] == "false"


def test_reminder_still_goes_out_if_the_note_cannot_be_added(fake_paypal, demo_day):
    inv = _bill(fake_paypal, due=_soon(0))
    fake_paypal.fail_next[f"/v2/invoicing/invoices/{inv['id']}"] = PayPalError(500, {"name": "INTERNAL"})
    demo_day(1); chasing.chase_due()
    assert len(fake_paypal.reminders) == 1


# ---------- payment landed ----------

def test_payment_landed_suggests_tax_only(client, fake_paypal):
    a = _bill(fake_paypal, amount=300)
    fake_paypal.client_pays(a["id"])
    st = client.post("/api/getpaid/check").json()
    landed = next(e["message"] for e in st["log"] if e["event_type"] == "UPSTREAM.INVOICE.PAID")
    assert "Set aside about $75.00 for tax." in landed and "open source" not in landed
    assert "pot" not in st
