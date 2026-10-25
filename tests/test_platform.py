"""Everything around the modules: health, scheduler, database upgrades, AI retries, webhooks,
donations, the basic invoice routes and the frontend's JavaScript."""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from backend import config, db, payroll, scheduler
from backend.agent import llm

ROOT = Path(__file__).resolve().parent.parent


def test_health_reports_configuration(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["paypal_base"].endswith("sandbox.paypal.com")
    assert h["llm"] is False and "pot" not in h and h["payroll_min_edit_minutes"] == 5


def test_dashboard_and_static_files_are_served(client):
    assert "Upstream" in client.get("/").text
    for f in ("app.js", "team.js", "getpaid.js", "styles.css"):
        assert client.get(f"/static/{f}").status_code == 200


def test_one_failing_job_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(payroll, "tick", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = scheduler.tick_all()
    assert out["payroll"] == ["error: boom"] and out["chasing"] == [] and out["retainers"] == []


def test_tick_route(client):
    assert set(client.post("/api/tick").json()) == {"payroll", "chasing", "retainers"}


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="SQLite file upgrade")
def test_old_databases_are_upgraded_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, number TEXT, client_name TEXT, "
                "client_email TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, "
                "due_date TEXT NOT NULL, status TEXT NOT NULL, payer_url TEXT, items TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, paid_at TEXT, "
                "chase_stage INTEGER NOT NULL DEFAULT 0, last_reminder_at TEXT)")
    con.execute("INSERT INTO invoices VALUES ('I1','0001','Acme','a@x.com',5,'USD','2026-01-01',"
                "'SENT',NULL,'[]','t','t',NULL,0,NULL)")
    con.commit(); con.close()
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    db.init()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(invoices)")}
    assert {"needs_you_at", "amount_due", "written_off", "viewed"} <= cols
    assert db.get_invoice("I1")["amount"] == 5  # existing data kept


def test_demo_clients_are_seeded():
    assert db.find_client("acme")["email"] == "acme@example.com"
    assert db.find_client("glob")["name"] == "Globex"     # unique partial match


# ---------- the AI client ----------

class _R:
    def __init__(self, status, text="", data=None, headers=None):
        self.status_code, self.text, self._data, self.headers = status, text, data, headers or {}

    def json(self):
        return self._data


@pytest.fixture
def llm_http(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    waits, replies = [], []
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **kw: replies.pop(0))
    return waits, replies


def test_ai_waits_and_retries_when_rate_limited(llm_http):
    waits, replies = llm_http
    replies += [_R(429, "Please try again in 607.5ms."), _R(200, data={"choices": [{"message": {"content": "ok"}}]})]
    assert llm.complete("hi") == "ok" and 0.6 < waits[0] < 1.5


def test_ai_gives_up_on_long_waits_and_errors(llm_http):
    waits, replies = llm_http
    replies.append(_R(429, "try again in 90s"))
    with pytest.raises(llm.LLMError, match="rate limit"):
        llm.complete("hi")
    replies.append(_R(400, "bad request"))
    with pytest.raises(llm.LLMError, match="400"):
        llm.chat([{"role": "user", "content": "x"}])
    assert waits == []


def test_ai_without_a_key_fails_clearly(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    with pytest.raises(llm.LLMError, match="not set"):
        llm.complete("hi")


# ---------- webhooks ----------

def test_webhook_signature_is_verified_with_paypal(client, fake_paypal, monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_WEBHOOK_ID", "WH-1")
    client.post("/webhooks/paypal", json={"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS", "resource": {}})
    assert client.get("/api/events").json()[0]["verified"] is True
    assert any(path == "/v1/notifications/verify-webhook-signature" for _, path, _ in fake_paypal.calls)


# ---------- donations (Module 4 so far) and the basic invoice routes ----------

def test_donation_goes_to_the_hospital(client, fake_paypal):
    r = client.post("/api/donations", json={"amount": 20, "hospital_email": "hospital@example.com",
                                            "campaign": "Surgery bill"}).json()
    assert r["donor_approval_link"] and r["status"] == "PAYER_ACTIVITY_REQUIRED" or r["order_id"]
    order = fake_paypal.orders[r["order_id"]]
    assert order["purchase_units"][0]["payee"]["email_address"] == "hospital@example.com"
    page = client.get(f"/donation/return?token={r['order_id']}").text
    assert "hospital@example.com" in page and "COMPLETED" in page
    assert "Missing order" in client.get("/donation/return").text


def test_basic_invoice_routes(client, fake_paypal):
    inv = client.post("/api/invoices", json={"client_email": "c@example.com", "client_name": "C",
                                             "items": [{"name": "Work", "unit_price": 50}]}).json()
    assert inv["status"] == "SENT" and inv["amount"] == "50.00"
    assert client.get("/api/invoices/open").json()[0]["id"] == inv["id"]
    assert client.post(f"/api/invoices/{inv['id']}/remind", json={"note": "Reminder"}).json()["ok"]
    assert client.post(f"/api/invoices/{inv['id']}/cancel").json()["ok"]
    assert fake_paypal.invoices[inv["id"]]["status"] == "CANCELLED"


# ---------- the frontend ----------

@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
@pytest.mark.parametrize("script", ["app.js", "team.js", "getpaid.js"])
def test_frontend_javascript_has_no_syntax_errors(script):
    r = subprocess.run(["node", "--check", str(ROOT / "frontend" / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_schema_splits_into_whole_statements_for_postgres():
    # PostgreSQL runs the schema one statement at a time, split on ";" (SQLite doesn't split,
    # so a ";" inside a comment only breaks Postgres, e.g. a Render deploy on Neon)
    for chunk in db.SCHEMA.split(";"):
        code = " ".join(line.partition("--")[0] for line in chunk.splitlines()).strip()
        assert not code or code.upper().startswith(("CREATE ", "ALTER ")), code[:80]


def test_page_and_scripts_are_never_served_stale(client):
    for path in ("/", "/static/team.js"):
        assert client.get(path).headers["cache-control"] == "no-cache"
