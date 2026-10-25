"""API routes for monthly team payroll (see backend/payroll.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import config, db, payroll
from backend.paypal import payouts

router = APIRouter(prefix="/api/payroll", tags=["Team payroll"])


def _run_summary(run: dict | None) -> dict | None:
    if not run:
        return None
    lines = [r for r in run["result"]["contributors"] if r.get("amount", 0) > 0]
    return {**run, "total": round(sum(r["amount"] for r in lines), 2), "people": len(lines)}


def _schedule_view(s: dict) -> dict:
    now = datetime.now(timezone.utc)
    runs = db.runs_for_schedule(s["id"])
    return {**s, "next_payday": payroll.next_payday(s, now).isoformat(),
            "current_run": _run_summary(next((r for r in runs if r["status"] in
                                              ("preview", "paying")), None)),
            "runs": [_run_summary(r) for r in runs]}


class PreviewIn(BaseModel):
    repo: str = Field(..., examples=["expressjs/express"])
    owner_login: str = Field(..., description="Your GitHub login; your own commits aren't paid.")
    total: float = Field(..., gt=0)
    top_n: int = Field(5, ge=1, le=50)
    days: int = Field(30, ge=1, le=90, description="How far back to look for this preview.")


@router.post("/preview")
def preview(body: PreviewIn):
    if body.total > config.PAYROLL_MONTHLY_CAP:
        raise HTTPException(400, f"The payroll cap is ${config.PAYROLL_MONTHLY_CAP:.2f} a month.")
    now = datetime.now(timezone.utc)
    try:
        return payroll.compute_split(body.repo, body.owner_login, body.total, body.top_n,
                                     now - timedelta(days=body.days), now)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ScheduleIn(BaseModel):
    repo: str
    owner_login: str
    total: float = Field(..., gt=0, description="Amount each payday.")
    frequency: str = Field("monthly", pattern="^(daily|weekly|monthly)$")
    pay_day: int = Field(1, ge=0, le=28, description="Monthly: day of the month (1-28). "
                                                     "Weekly: 0 = Monday ... 6 = Sunday.")
    pay_time: str = Field("00:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["21:30"])
    tz: str = Field("UTC", examples=["Asia/Kolkata"], description="Your time zone.")
    top_n: int = Field(5, ge=1, le=50)
    notice_days: int = Field(2, ge=0, le=7, description="Heads-up before each payday.")
    notice_hours: float | None = Field(None, ge=0, le=168, description="Heads-up in hours "
                                       "(overrides notice_days; must be under a day for daily).")
    emails: dict[str, str] = Field(default_factory=dict)
    approved: bool = False


@router.post("/schedules")
def create_schedule(body: ScheduleIn):
    if not body.approved:
        raise HTTPException(400, "Automatic payroll moves money out every month, so it needs "
                                 "your standing approval (approved: true).")
    if body.total > config.PAYROLL_MONTHLY_CAP:
        raise HTTPException(400, f"The payroll cap is ${config.PAYROLL_MONTHLY_CAP:.2f} a month.")
    try:
        payroll.zone(body.tz)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.frequency == "monthly" and not 1 <= body.pay_day <= 28:
        raise HTTPException(400, "Monthly pay day must be 1-28.")
    if body.frequency == "weekly" and not 0 <= body.pay_day <= 6:
        raise HTTPException(400, "Weekly pay day must be 0 (Monday) to 6 (Sunday).")
    minutes = round(body.notice_hours * 60) if body.notice_hours is not None else None
    if body.frequency == "daily" and (minutes if minutes is not None else body.notice_days * 1440) >= 1440:
        raise HTTPException(400, "A daily payroll's heads-up must be shorter than a day.")
    sid = db.create_schedule({**body.model_dump(exclude={"approved", "notice_hours"}),
                              "notice_minutes": minutes,
                              "owner_login": body.owner_login.lstrip("@")})
    # Make this month's preview now if the heads-up has started; never pay in this call
    payroll.tick(pay=False)
    return _schedule_view(db.get_schedule(sid))


@router.get("/schedules")
def list_schedules():
    return [_schedule_view(s) for s in db.list_schedules()]


def _get_schedule(sid: str) -> dict:
    s = db.get_schedule(sid)
    if not s:
        raise HTTPException(404, "Schedule not found.")
    return s


@router.post("/schedules/{sid}/pause")
def pause(sid: str):
    _get_schedule(sid)
    db.update_schedule(sid, active=False)
    return _schedule_view(db.get_schedule(sid))


@router.post("/schedules/{sid}/resume")
def resume(sid: str):
    _get_schedule(sid)
    db.update_schedule(sid, active=True)
    return _schedule_view(db.get_schedule(sid))


class EmailsIn(BaseModel):
    emails: dict[str, str]


@router.put("/schedules/{sid}/emails")
def set_emails(sid: str, body: EmailsIn):
    s = _get_schedule(sid)
    emails = {**s["emails"], **{k.lstrip("@"): v.strip() for k, v in body.emails.items()}}
    db.update_schedule(sid, emails=emails)
    # Apply to a waiting run too, so this month uses them
    for run in db.runs_for_schedule(sid):
        if run["status"] == "preview":
            for r in run["result"]["contributors"]:
                if r["login"] in emails:
                    r["email"] = emails[r["login"]]
            db.update_run(run["id"], expect_status="preview", result=run["result"])
    return _schedule_view(db.get_schedule(sid))


def _get_run(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Payroll run not found.")
    return run


class AmountsIn(BaseModel):
    revision: int
    amounts: dict[str, float]


@router.put("/runs/{run_id}/amounts")
def edit_amounts(run_id: str, body: AmountsIn):
    run = _get_run(run_id)
    if run["status"] != "preview":
        raise HTTPException(409, f"This run is already {run['status']}.")
    if body.revision != run["revision"]:
        raise HTTPException(409, f"Revision {body.revision} is out of date; the latest is "
                                 f"{run['revision']}. Reload and try again.")
    people = {r["login"]: r for r in run["result"]["contributors"]}
    unknown = [k for k in body.amounts if k not in people]
    if unknown:
        raise HTTPException(400, f"Not in this run: {', '.join(unknown)}")
    if any(v < 0 for v in body.amounts.values()):
        raise HTTPException(400, "Amounts can't be negative.")
    for login, v in body.amounts.items():
        if round(v, 2) != people[login]["amount"]:
            people[login]["amount"], people[login]["user_edited"] = round(v, 2), True
    total = round(sum(r["amount"] for r in people.values()), 2)
    if total > config.PAYROLL_MONTHLY_CAP:
        raise HTTPException(400, f"Total ${total:.2f} exceeds the payroll cap of "
                                 f"${config.PAYROLL_MONTHLY_CAP:.2f}.")
    for r in people.values():
        r["percent"] = round(100 * r["amount"] / total, 1) if total else 0.0
    run["result"]["contributors"].sort(key=lambda r: -r["amount"])
    if not db.update_run(run_id, expect_status="preview", result=run["result"],
                         revision=run["revision"] + 1):
        raise HTTPException(409, "This run changed while you were editing. Reload it.")
    return _run_summary(db.get_run(run_id))


@router.post("/runs/{run_id}/skip")
def skip(run_id: str):
    _get_run(run_id)
    try:
        return _run_summary(payroll.skip_run(run_id))
    except ValueError as e:
        raise HTTPException(409, str(e))


class PayNowIn(BaseModel):
    revision: int
    approved: bool = False


@router.post("/runs/{run_id}/pay-now")
def pay_now(run_id: str, body: PayNowIn):
    """Pay before payday. An explicit yes for this exact version of the split."""
    run = _get_run(run_id)
    if not body.approved:
        raise HTTPException(400, "Paying early needs explicit approval (approved: true).")
    if body.revision != run["revision"]:
        raise HTTPException(409, f"You approved revision {body.revision}, but the latest is "
                                 f"{run['revision']}. Review it and approve again.")
    try:
        return _run_summary(payroll.pay_run(run_id))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/tick")
def tick():
    """Run the scheduler once. Safe to call publicly: it only does what the schedules allow
    (previews in the heads-up window, payments on or after payday), and never pays twice."""
    return {"actions": payroll.tick()}


@router.get("/batches/{batch_id}")
def batch_status(batch_id: str):
    """Who has received their pay: refreshed from PayPal (the fallback when webhooks are slow
    or not set up)."""
    if not db.get_batch(batch_id):
        raise HTTPException(404, "Batch not found.")
    data = payouts.get_batch(batch_id)
    db.update_batch_status(batch_id, (data.get("batch_header") or {}).get("batch_status"))
    for i in data.get("items", []):
        sender_item_id = (i.get("payout_item") or {}).get("sender_item_id")
        if sender_item_id:
            db.update_item(batch_id, sender_item_id, status=i.get("transaction_status"),
                           payout_item_id=i.get("payout_item_id"))
    return db.get_batch(batch_id)
