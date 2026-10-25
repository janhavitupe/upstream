"""Team payroll scheduling: pays on payday, after the heads-up, never twice."""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend import db, payroll


def test_pay_date_clamps_to_month_length():
    assert payroll.pay_date(2026, 2, 28) == date(2026, 2, 28)
    assert payroll.pay_date(2026, 4, 28) == date(2026, 4, 28)


def _schedule(pay_day, notice=2, created=None, **timing):
    sid = db.create_schedule({"repo": "o/r", "owner_login": "me", "total": 10, "pay_day": pay_day,
                              "top_n": 3, "notice_days": notice, **timing})
    if created:
        db.update_schedule(sid, created_at=created)
    return db.get_schedule(sid)


def test_schedule_started_after_this_months_payday_waits_for_next_month():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    s = _schedule(5, created="2026-10-20T00:00:00+00:00")
    assert [p for p, _ in payroll.paydays(s, now)] == ["2026-11"]  # October's is in the past
    assert payroll.next_payday(s, now) == datetime(2026, 11, 5, tzinfo=timezone.utc)


@pytest.fixture
def fake_split(monkeypatch):
    def split(repo, owner, total, top_n, since, until, emails=None):
        return {"repo": repo, "owner_login": owner, "window_start": payroll._iso(since),
                "window_end": payroll._iso(until), "total": total, "sandbox": True, "warnings": [],
                "contributors": [{"login": "dev1", "amount": total, "percent": 100.0,
                                  "commits": 3, "email": "dev1@example.com", "explanation": "x"}]}
    monkeypatch.setattr(payroll, "compute_split", split)


@pytest.fixture
def paid(monkeypatch):
    batches = []
    monkeypatch.setattr(payroll.payouts, "create_batch", lambda items, **kw: (
        batches.append((items, kw)) or
        {"batch_header": {"payout_batch_id": f"B{len(batches)}", "batch_status": "PENDING"}}))
    return batches


def test_heads_up_first_then_pays_on_payday_once(fake_split, paid):
    s = _schedule(25, notice=2, created="2026-10-01T00:00:00+00:00")
    t = lambda d: datetime(2026, 10, d, 12, tzinfo=timezone.utc)  # noqa: E731
    assert payroll.tick(t(22)) == []                                 # before the heads-up
    assert payroll.tick(t(23)) == ["preview o/r 2026-10"]            # heads-up window
    assert payroll.tick(t(24)) == [] and not paid                    # still waiting
    assert payroll.tick(t(26)) == ["paid o/r 2026-10"]
    assert payroll.tick(t(27)) == [] and len(paid) == 1             # never twice
    run = db.runs_for_schedule(s["id"])[0]
    assert run["status"] == "paid" and paid[0][1]["sender_batch_id"] == f"payroll-{run['id']}"


def test_creating_a_schedule_never_pays_in_the_same_call(fake_split, paid):
    now = datetime.now(timezone.utc)
    if now.day > 28:
        pytest.skip("pay days are 1-28")
    _schedule(min(now.day, 28), notice=0, created=(now - timedelta(days=1)).isoformat())
    assert payroll.tick(now, pay=False) and not paid


def test_skipped_and_paused_schedules_do_not_pay(fake_split, paid):
    s = _schedule(25, notice=2, created="2026-10-01T00:00:00+00:00")
    payroll.tick(datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
    payroll.skip_run(db.runs_for_schedule(s["id"])[0]["id"])
    assert payroll.tick(datetime(2026, 10, 26, tzinfo=timezone.utc)) == [] and not paid
    db.update_schedule(s["id"], active=False)
    assert payroll.tick(datetime(2026, 11, 25, tzinfo=timezone.utc)) == []


IST = "Asia/Kolkata"


def test_daily_at_930_pm_ist_pays_every_evening(fake_split, paid):
    # 21:30 IST is 16:00 UTC; heads-up 1 hour before
    s = _schedule(0, created="2026-10-26T14:00:00+00:00", frequency="daily", pay_time="21:30",
                  tz=IST, notice_minutes=60)
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert payroll.tick(at(26, 14, 30)) == []                          # before the heads-up
    assert payroll.tick(at(26, 15, 0)) == ["preview o/r 2026-10-26"]   # 20:30 IST
    assert payroll.tick(at(26, 15, 59)) == [] and not paid             # 21:29 IST
    assert payroll.tick(at(26, 16, 0)) == ["paid o/r 2026-10-26"]      # 21:30 IST
    assert payroll.tick(at(26, 18, 0)) == []                           # never twice
    assert payroll.next_payday(db.get_schedule(s["id"]), at(26, 18)) == at(27, 16)
    assert payroll.tick(at(27, 15, 0)) == ["preview o/r 2026-10-27"]   # the next evening
    assert payroll.tick(at(27, 16, 5)) == ["paid o/r 2026-10-27"] and len(paid) == 2


def test_weekly_payday_and_heads_up_across_midnight(fake_split, paid):
    # Every Monday at 09:00 IST (03:30 UTC), heads-up 12 hours: starts Sunday evening
    _schedule(0, created="2026-10-20T00:00:00+00:00", frequency="weekly", pay_time="09:00",
              tz=IST, notice_minutes=720)
    sunday_night = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)   # Sun 21:30 IST
    assert payroll.tick(sunday_night) == ["preview o/r 2026-W44"]
    assert payroll.tick(datetime(2026, 10, 26, 3, 29, tzinfo=timezone.utc)) == []
    assert payroll.tick(datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)) == ["paid o/r 2026-W44"]


def test_monthly_heads_up_starts_in_the_month_before(fake_split, paid):
    # Pay day 1, heads-up 2 days: the preview comes on Oct 30, not on Nov 1
    _schedule(1, notice=2, created="2026-10-05T00:00:00+00:00")
    assert payroll.tick(datetime(2026, 10, 30, 1, tzinfo=timezone.utc)) == ["preview o/r 2026-11"]
    assert payroll.tick(datetime(2026, 11, 1, 1, tzinfo=timezone.utc)) == ["paid o/r 2026-11"]


def test_only_one_open_payday_at_a_time(fake_split, paid):
    # Daily with a 23-hour heads-up: tomorrow's preview waits until today's is paid or skipped,
    # so the same work is never in two runs
    s = _schedule(0, created="2026-10-26T00:00:00+00:00", frequency="daily", pay_time="12:00",
                  notice_minutes=23 * 60)
    payroll.tick(datetime(2026, 10, 26, 11, tzinfo=timezone.utc), pay=False)
    payroll.tick(datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc), pay=False)
    assert [r["period"] for r in db.runs_for_schedule(s["id"])] == ["2026-10-26"]


def test_schedule_api_validates_timing(client, fake_split):
    base = {"repo": "o/r", "owner_login": "me", "total": 5, "approved": True}
    assert client.post("/api/payroll/schedules", json={**base, "tz": "Mars/Base"}).status_code == 400
    assert client.post("/api/payroll/schedules",
                       json={**base, "frequency": "weekly", "pay_day": 9}).status_code == 400
    assert client.post("/api/payroll/schedules",
                       json={**base, "frequency": "daily", "notice_hours": 30}).status_code == 400
    assert client.post("/api/payroll/schedules", json={**base, "pay_time": "9:30pm"}).status_code == 422
    ok = client.post("/api/payroll/schedules", json={**base, "frequency": "daily", "pay_time": "21:30",
                                                     "tz": IST, "notice_hours": 1})
    assert ok.status_code == 200 and ok.json()["frequency"] == "daily"
    assert ok.json()["notice_minutes"] == 60 and ok.json()["tz"] == IST
