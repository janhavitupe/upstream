"""Test setup: a throwaway database per test, no network, no background timer.

Environment variables are set before anything imports backend.config, and load_dotenv never
overrides variables that already exist, so real keys in .env are never used by the tests.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "DATABASE_PATH": str(Path(tempfile.mkdtemp()) / "test.db"),
    "PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": "", "PAYPAL_WEBHOOK_ID": "",
    "PAYPAL_BASE": "https://api-m.sandbox.paypal.com",
    "LLM_API_KEY": "", "GITHUB_TOKEN": "", "SMTP_HOST": "",
    "PAYROLL_SCHEDULER": "0", "RENDER": "",
    "DEMO_MAINTAINER_EMAILS": "m-a@example.com,m-b@example.com",
    "DEMO_CLIENTS": "Acme=acme@example.com,Globex=globex@example.com",
    "OSS_MONTHLY_CAP": "500", "PAYROLL_MONTHLY_CAP": "2000",
})

from backend import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets its own empty database."""
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "upstream.db")
    db.init()
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP call fails the test loudly instead of reaching PayPal or GitHub."""
    import requests

    def blocked(*a, **kw):
        raise AssertionError(f"Unexpected network call in a test: {a[:2]}")
    monkeypatch.setattr(requests.Session, "request", blocked)
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.main import app
    with TestClient(app) as c:
        yield c
