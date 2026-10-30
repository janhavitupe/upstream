"""The Testing guide: sandbox logins, live task ticks and the sample bills."""
import hashlib
import io

from pypdf import PdfReader

from backend import config, db


def _text(pdf: bytes) -> str:
    return PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


def test_sandbox_logins_are_shown(client):
    logins = client.get("/api/guide").json()["logins"]
    assert len(logins) == 9 and all(a["email"] and a["password"] and a["role"] for a in logins)


def test_logins_are_never_shown_outside_the_sandbox(client, monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_BASE", "https://api-m.paypal.com")
    assert client.get("/api/guide").json()["logins"] == []


def test_tasks_start_undone_and_tick_from_real_records(client):
    tasks = client.get("/api/guide").json()["tasks"]
    assert len(tasks) == 11 and not any(tasks.values())
    db.set_setting("voice_samples", ["Hi there"])
    tasks = client.get("/api/guide").json()["tasks"]
    assert tasks["voice_saved"] and not tasks["invoice_sent"]


def test_sample_bills_download_as_fresh_readable_pdfs(client):
    real = client.get("/api/causes/sample-bill/real")
    assert real.headers["content-type"] == "application/pdf"
    assert 'filename="real-bill.pdf"' in real.headers["content-disposition"]
    assert "Sam Lee" in _text(real.content) and "5,100.00" in _text(real.content)
    assert "Jordan Park" in _text(client.get("/api/causes/sample-bill/fake").content)
    again = client.get("/api/causes/sample-bill/real").content
    assert hashlib.sha256(again).digest() != hashlib.sha256(real.content).digest()


def test_unknown_sample_bill_is_not_found(client):
    assert client.get("/api/causes/sample-bill/other").status_code == 404
