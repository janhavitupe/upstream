"""The Guide tab: sandbox logins so testers can see the money move, and which tasks have been done."""
from __future__ import annotations

from fastapi import APIRouter

from backend import config, db

router = APIRouter()

# email, who it is, what to look for after logging in. Passwords come from SANDBOX_LOGINS.
ACCOUNTS = [
    ("upstream-merchant@business.example.com", "You, the business",
     "Invoices sent, payments received and team payouts"),
    ("acme-client@personal.example.com", "Client: Acme", "Pay an invoice or approve a retainer"),
    ("globex-client@personal.example.com", "Client: Globex", "Pay an invoice or approve a retainer"),
    ("maintainer-a@personal.example.com", "Team member A", "Salary received"),
    ("maintainer-b@personal.example.com", "Team member B", "Salary received"),
    ("maintainer-c@personal.example.com", "Team member C", "Salary received"),
    ("city-hospital@business.example.com", "City General Hospital", "Donations received directly"),
    ("donor-one@personal.example.com", "Donor 1", "Make a donation"),
    ("donor-two@personal.example.com", "Donor 2", "Make a donation"),
]


def logins() -> list[dict]:
    """Passwords are only ever shown for sandbox accounts."""
    if "sandbox" not in config.PAYPAL_BASE:
        return []
    return [{"email": email, "password": config.SANDBOX_LOGINS[email], "role": role, "check": check}
            for email, role, check in ACCOUNTS if email in config.SANDBOX_LOGINS]


def tasks() -> dict[str, bool]:
    """What has really happened on this app, read from its own records."""
    schedules = db.list_schedules()
    runs = [r for s in schedules for r in db.runs_for_schedule(s["id"])]
    invoices = db.list_invoices()
    retainers = db.list_retainers()
    campaigns = db.list_campaigns()
    return {
        "payroll_started": bool(schedules),
        "payroll_paid": any(r["status"] == "paid" for r in runs),
        "invoice_sent": bool(invoices),
        "reminder_sent": any(i["chase_stage"] for i in invoices),
        "invoice_paid": any(i["status"] in ("PAID", "MARKED_AS_PAID") for i in invoices),
        "voice_saved": bool(db.get_setting("voice_samples", [])),
        "retainer_created": bool(retainers),
        "retainer_active": any(r["status"] == "ACTIVE" for r in retainers),
        "bill_blocked": any(c["verdict"] != "verified" for c in campaigns),
        "bill_verified": any(c["verdict"] == "verified" for c in campaigns),
        "donation_paid": any(db.campaign_raised(c["id"])[1] for c in campaigns),
    }


@router.get("/api/guide", tags=["meta"])
def guide():
    return {"logins": logins(), "tasks": tasks()}
