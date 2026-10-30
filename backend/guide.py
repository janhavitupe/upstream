"""The Guide tab: sandbox logins so testers can see the money move, and which tasks have been done."""
from __future__ import annotations

from fastapi import APIRouter

from backend import config, db

router = APIRouter()

# PayPal sandbox test accounts (play money), shown on the Testing guide tab on purpose.
# email, password, who it is, what to look for after logging in
ACCOUNTS = [
    ("upstream-merchant@business.example.com", "Upstr3am!Sb1", "You, the business",
     "Invoices sent, payments received and team payouts"),
    ("acme-client@personal.example.com", "Upstr3am!Sb2", "Client: Acme",
     "Pay an invoice or approve a retainer"),
    ("globex-client@personal.example.com", "Upstr3am!Sb3", "Client: Globex",
     "Pay an invoice or approve a retainer"),
    ("maintainer-a@personal.example.com", "Upstr3am!Sb4", "Team member A", "Salary received"),
    ("maintainer-b@personal.example.com", "ABCQWERTY", "Team member B", "Salary received"),
    ("maintainer-c@personal.example.com", "Upstr3am!Sb6", "Team member C", "Salary received"),
    ("city-hospital@business.example.com", "Upstr3am!Sb7", "City General Hospital",
     "Donations received directly"),
    ("donor-one@personal.example.com", "Upstr3am!Sb8", "Donor 1", "Make a donation"),
    ("donor-two@personal.example.com", "Upstr3am!Sb9", "Donor 2", "Make a donation"),
]


def logins() -> list[dict]:
    """Only ever shown when the app runs on the sandbox."""
    if "sandbox" not in config.PAYPAL_BASE:
        return []
    return [{"email": email, "password": password, "role": role, "check": check}
            for email, password, role, check in ACCOUNTS]


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
