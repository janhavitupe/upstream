"""Central configuration, loaded from the .env file at the project root."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


# PayPal (sandbox)
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com").rstrip("/")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()

# GitHub (optional token raises the rate limit from 60 to 5,000 requests/hour)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# LLM: any OpenAI-compatible API, Groq by default (optional: explanations fall back to
# rule-based text without a key)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b").strip()

# App
# Render sets RENDER and RENDER_EXTERNAL_URL on every deploy
IS_PUBLIC_DEPLOY = bool(os.getenv("RENDER"))
APP_BASE_URL = (os.getenv("APP_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "")
                or "http://localhost:8000").rstrip("/")
INVOICER_NAME = os.getenv("INVOICER_NAME", "Upstream Studio")
OSS_MONTHLY_CAP = float(os.getenv("OSS_MONTHLY_CAP", "500"))
# Team payroll: hard cap on any one month's salaries, and the scheduler
PAYROLL_MONTHLY_CAP = float(os.getenv("PAYROLL_MONTHLY_CAP", "2000"))
PAYROLL_SCHEDULER = os.getenv("PAYROLL_SCHEDULER", "1").strip() != "0"  # in-app timer
# Every heads-up leaves at least this long to edit or skip before it pays itself
PAYROLL_MIN_EDIT_MINUTES = float(os.getenv("PAYROLL_MIN_EDIT_MINUTES", "5"))
# Unclaimed payouts older than this are cancelled automatically and re-queued
UNCLAIMED_GRACE_DAYS = float(os.getenv("UNCLAIMED_GRACE_DAYS", "14"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "").strip() or ROOT / "data" / "upstream.db")

# Sandbox demo recipients
DEMO_MAINTAINER_EMAILS = _list("DEMO_MAINTAINER_EMAILS")
DEMO_CLIENT_EMAIL = os.getenv("DEMO_CLIENT_EMAIL", "")
DEMO_HOSPITAL_EMAIL = os.getenv("DEMO_HOSPITAL_EMAIL", "")
# Clients the chat knows by name: "Acme=acme@example.com,Globex=globex@example.com"
DEMO_CLIENTS = dict(pair.split("=", 1) for pair in _list("DEMO_CLIENTS") if "=" in pair) or (
    {"Acme": DEMO_CLIENT_EMAIL} if DEMO_CLIENT_EMAIL else {})
INVOICE_MAX = float(os.getenv("INVOICE_MAX", "25000"))  # sanity cap on one chat-made invoice


def paypal_configured() -> bool:
    return bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)


def llm_configured() -> bool:
    return bool(LLM_API_KEY)
