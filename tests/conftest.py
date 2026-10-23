"""Test setup: a throwaway database per test and no background timer.

Unit tests (the default) also get no network and blank keys: environment variables are set
before anything imports backend.config, and load_dotenv never overrides variables that already
exist, so real keys in .env are never used.

End-to-end tests (tests/e2e, marked e2e) run against the real PayPal sandbox. They only run when
UPSTREAM_E2E=1; keys then come from the environment (e.g. Jenkins credentials) or .env.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
E2E = os.environ.get("UPSTREAM_E2E") == "1"

os.environ.update({
    "DATABASE_PATH": str(Path(tempfile.mkdtemp()) / "test.db"),
    "PAYPAL_BASE": "https://api-m.sandbox.paypal.com",  # tests never touch live PayPal
    "PAYROLL_SCHEDULER": "0", "RENDER": "", "SMTP_HOST": "", "PAYPAL_WEBHOOK_ID": "",
    "DATABASE_URL": "",  # never the real database: tests wipe theirs before every test
    "OSS_MONTHLY_CAP": "500", "PAYROLL_MONTHLY_CAP": "2000",
})
if not E2E:
    os.environ.update({
        "PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": "", "LLM_API_KEY": "", "GITHUB_TOKEN": "",
        "DEMO_MAINTAINER_EMAILS": "m-a@example.com,m-b@example.com",
        "DEMO_CLIENTS": "Acme=acme@example.com,Globex=globex@example.com",
    })

from backend import config, db  # noqa: E402


# Optional: run the whole suite on PostgreSQL too, e.g.
#   TEST_DATABASE_URL=postgresql://user:pw@localhost/upstream_test python -m pytest
TEST_PG = os.environ.get("TEST_DATABASE_URL", "").strip()
if TEST_PG and "test" not in TEST_PG.rsplit("/", 1)[-1].split("?")[0]:
    raise RuntimeError("TEST_DATABASE_URL must point at a database whose name contains 'test': "
                       "every test wipes it.")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets its own empty database (a temp SQLite file, or a wiped test Postgres)."""
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "upstream.db")
    monkeypatch.setattr(config, "DATABASE_URL", TEST_PG)
    if TEST_PG:
        import psycopg
        with psycopg.connect(TEST_PG, autocommit=True) as c:
            c.execute("DROP SCHEMA public CASCADE")
            c.execute("CREATE SCHEMA public")
    db.init()
    yield


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """In unit tests, any real HTTP call fails loudly instead of reaching PayPal or GitHub."""
    if request.node.get_closest_marker("e2e"):
        yield
        return
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


@pytest.fixture
def fake_paypal(monkeypatch):
    """A stateful in-memory PayPal sandbox (see tests/fakes.py)."""
    from backend.paypal.client import paypal
    from tests.fakes import FakePayPal
    fake = FakePayPal()
    monkeypatch.setattr(paypal, "request", fake.request)
    return fake


@pytest.fixture
def fake_llm(monkeypatch):
    """A scripted AI: queue replies or tool calls; every request is recorded."""
    from backend.agent import llm
    from tests.fakes import FakeLLM
    fake = FakeLLM()
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_post", fake)
    return fake


@pytest.fixture
def fake_github(monkeypatch):
    """GitHub served from dicts: set fake.api[path], fake.files[(owner, repo, path)], fake.trees."""
    from backend.oss import github
    from tests.fakes import FakeGitHub
    fake = FakeGitHub()
    monkeypatch.setattr(github, "api_get", fake.api_get)
    monkeypatch.setattr(github, "raw_file", fake.raw_file)
    monkeypatch.setattr(github, "get_tree", fake.get_tree)
    return fake


@pytest.fixture
def demo_day(monkeypatch):
    """Move the (sandbox) demo clock: demo_day(8) pretends it's 8 days later."""
    def move(days):
        db.set_setting("demo_offset_days", days)
    return move
