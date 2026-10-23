"""Team payroll scheduling: pays on payday, after the heads-up, never twice."""
from datetime import date, datetime, timedelta, timezone

import pytest

from backend import db, payroll


def test_pay_date_clamps_to_month_length():
    assert payroll.pay_date(2026, 2, 28) == date(2026, 2, 28)
    assert payroll.pay_date(2026, 4, 28) == date(2026, 4, 28)


def _schedule(pay_day, notice=2, created=None):
    sid = db.create_schedule({"repo": "o/r", "owner_login": "me", "total": 10, "pay_day": pay_day,
                              "top_n": 3, "notice_days": notice})
    if created:
        db.update_schedule(sid, created_at=created)
    return db.get_schedule(sid)


def test_schedule_started_after_this_months_payday_waits_for_next_month():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    s = _schedule(5, created="2026-10-20T00:00:00+00:00")
    assert payroll.due_period(s, now) is None
    assert payroll.next_payday(s, now) == date(2026, 11, 5)


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
    _schedule(min(now.day, 28), notice=0, created=(now - timedelta(days=1)).isoformat())
    assert payroll.tick(now, pay=False) and not paid


def test_skipped_and_paused_schedules_do_not_pay(fake_split, paid):
    s = _schedule(25, notice=2, created="2026-10-01T00:00:00+00:00")
    payroll.tick(datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
    payroll.skip_run(db.runs_for_schedule(s["id"])[0]["id"])
    assert payroll.tick(datetime(2026, 10, 26, tzinfo=timezone.utc)) == [] and not paid
    db.update_schedule(s["id"], active=False)
    assert payroll.tick(datetime(2026, 11, 25, tzinfo=timezone.utc)) == []
