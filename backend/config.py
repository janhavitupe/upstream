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

# LLM (optional: explanations fall back to rule-based text without a key)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()

# App
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
INVOICER_NAME = os.getenv("INVOICER_NAME", "Upstream Studio")
OSS_MONTHLY_CAP = float(os.getenv("OSS_MONTHLY_CAP", "500"))

# Sandbox demo recipients
DEMO_MAINTAINER_EMAILS = _list("DEMO_MAINTAINER_EMAILS")
DEMO_CLIENT_EMAIL = os.getenv("DEMO_CLIENT_EMAIL", "")
DEMO_HOSPITAL_EMAIL = os.getenv("DEMO_HOSPITAL_EMAIL", "")


def paypal_configured() -> bool:
    return bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)
