"""Pay your team: who did what, the split, payment safety, and every payroll route."""
from datetime import datetime, timedelta, timezone

import pytest

from backend import config, db, payroll
from backend.paypal.client import PayPalError

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
WEEK = int((NOW - timedelta(days=10)).timestamp())


def _commit(login, msg="feat: work", name="", parents=1):
    return {"author": {"login": login} if login else None,
            "commit": {"author": {"name": name}, "message": msg}, "parents": [{}] * parents}


@pytest.fixture
def team_repo(fake_github):
    fake_github.api["/repos/me/app"] = {"owner": {"login": "me"}, "name": "app"}
    fake_github.api["/repos/me/app/commits"] = [
        _commit("alice", "feat: add search"), _commit("alice", "fix: crash"), _commit("alice"),
        _commit("bob", "docs: typo"), _commit("me", "chore: release"),           # owner
        _commit("dependabot[bot]", "bump x"), _commit("carol", parents=2),         # bot, merge
        _commit(None, name="Mystery Person")]                                       # no account
    fake_github.api["/repos/me/app/stats/contributors"] = [
        {"author": {"login": "alice"}, "weeks": [{"w": WEEK, "a": 300, "d": 100, "c": 3},
                                                  {"w": WEEK - 90 * 86400, "a": 9999, "d": 0, "c": 50}]},
        {"author": {"login": "bob"}, "weeks": [{"w": WEEK, "a": 3, "d": 1, "c": 1}]}]
    return fake_github


def test_contributor_work_excludes_owner_bots_merges_and_unlinked(team_repo):
    people, warnings = payroll.contributor_work("me", "app", NOW - timedelta(days=30), NOW, {"me"})
    assert set(people) == {"alice", "bob"}
    assert people["alice"]["commits"] == 3 and people["alice"]["lines_changed"] == 400  # old week ignored
    assert people["alice"]["subjects"][0] == "feat: add search"
    assert any("no linked GitHub account" in w for w in warnings)


def test_contributor_work_without_line_stats_warns(team_repo, monkeypatch):
    from backend import github
    monkeypatch.setattr(github, "contributor_stats", lambda o, r: None)
    people, warnings = payroll.contributor_work("me", "app", NOW - timedelta(days=30), NOW, set())
    assert people["alice"]["lines_changed"] is None and any("commits only" in w for w in warnings)


def test_split_pays_the_top_contributors_by_work(team_repo):
    s = payroll.compute_split("me/app", "@me", 100, 1, NOW - timedelta(days=30), NOW)
    assert [c["login"] for c in s["contributors"]] == ["alice"] and s["contributors"][0]["amount"] == 100
    assert s["others"] == [{"login": "bob", "commits": 1}] and s["owner_login"] == "me"
    assert any("top 1 of 2" in w for w in s["warnings"])
    both = payroll.compute_split("me/app", "me", 50, 5, NOW - timedelta(days=30), NOW)
    by = {c["login"]: c for c in both["contributors"]}
    assert by["alice"]["amount"] > by["bob"]["amount"] and round(sum(c["amount"] for c in both["contributors"]), 2) == 50
    assert by["alice"]["email"] in config.DEMO_TEAM_EMAILS  # sandbox stand-in


def test_split_ai_review_is_clamped(team_repo, fake_llm):
    fake_llm.reply('{"bob": {"explanation": "Docs.", "adjust_points": 50, "adjust_reason": "x"}}')
    s = payroll.compute_split("me/app", "me", 50, 5, NOW - timedelta(days=30), NOW)
    bob = next(c for c in s["contributors"] if c["login"] == "bob")
    assert s["explained_by"] == "llm" and bob["ai_adjust_points"] == 5


def test_split_with_nobody_to_pay(fake_github):
    fake_github.api["/repos/me/solo"] = {"owner": {"login": "me"}, "name": "solo"}
    fake_github.api["/repos/me/solo/commits"] = [_commit("me")]
    fake_github.api["/repos/me/solo/stats/contributors"] = []
    s = payroll.compute_split("me/solo", "me", 10, 3, NOW - timedelta(days=30), NOW)
    assert s["contributors"] == [] and any("nothing would be paid" in w for w in s["warnings"])


# ---------- paying a run ----------

def _run(contributors, status="preview"):
    sid = db.create_schedule({"repo": "me/app", "owner_login": "me", "total": 10, "pay_day": 25,
                              "top_n": 3, "notice_days": 2})
    run = {"id": "R1", "schedule_id": sid, "period": "2026-10", "status": status,
           "pay_after": "2026-10-25T00:00:00+00:00", "window_start": "2026-09-25T00:00:00+00:00",
           "window_end": "2026-10-23T00:00:00+00:00",
           "result": {"repo": "me/app", "sandbox": True, "contributors": contributors}}
    db.create_run(run)
    return sid


def test_pay_run_sends_one_batch_and_records_it(fake_paypal):
    sid = _run([{"login": "alice", "amount": 6, "email": "a@x.com", "explanation": "search"}])
    run = payroll.pay_run("R1")
    assert run["status"] == "paid" and run["batch_id"] in fake_paypal.batches
    item = next(iter(fake_paypal.items.values()))["payout_item"]
    assert "@alice" in item["note"] and item["amount"]["value"] == "6.00"
    assert db.get_schedule(sid)["last_window_end"] == "2026-10-23T00:00:00+00:00"
    with pytest.raises(ValueError, match="already paid"):
        payroll.pay_run("R1")


@pytest.mark.parametrize("contributors,error", [
    ([{"login": "a", "amount": 5000, "email": "a@x.com"}], "cap"),
    ([{"login": "a", "amount": 5, "email": ""}], "PayPal email"),
    ([{"login": "a", "amount": 0, "email": "a@x.com"}], "Nothing to pay"),
])
def test_pay_run_refuses_unsafe_runs(fake_paypal, contributors, error):
    _run(contributors)
    with pytest.raises(ValueError, match=error):
        payroll.pay_run("R1")
    assert not fake_paypal.batches


def test_failed_paypal_payment_goes_back_to_waiting(fake_paypal):
    _run([{"login": "a", "amount": 5, "email": "a@x.com"}])
    fake_paypal.fail_next["/v1/payments/payouts"] = PayPalError(500, {"name": "INTERNAL"})
    with pytest.raises(PayPalError):
        payroll.pay_run("R1")
    run = db.get_run("R1")
    assert run["status"] == "preview" and "failed" in run["note"]
    assert payroll.pay_run("R1")["status"] == "paid"  # the retry works


# ---------- the payroll API ----------

@pytest.fixture
def frozen_split(monkeypatch):
    def split(repo, owner, total, top_n, since, until, emails=None):
        return {"repo": repo, "owner_login": owner, "window_start": payroll._iso(since),
                "window_end": payroll._iso(until), "total": total, "sandbox": True, "warnings": [],
                "contributors": [
                    {"login": "alice", "amount": round(total * .7, 2), "percent": 70, "commits": 5,
                     "email": (emails or {}).get("alice", "m-a@example.com"), "explanation": "x"},
                    {"login": "bob", "amount": round(total * .3, 2), "percent": 30, "commits": 2,
                     "email": (emails or {}).get("bob", "m-b@example.com"), "explanation": "y"}]}
    monkeypatch.setattr(payroll, "compute_split", split)


def _create(client, **over):
    today = datetime.now(timezone.utc).day
    if today > 28 and "pay_day" not in over:
        pytest.skip("pay days are 1-28, so 'payday today' can't be tested on the 29th-31st")
    body = {"repo": "me/app", "owner_login": "me", "total": 10, "pay_day": today, "top_n": 2,
            "notice_days": 0, "approved": True}
    body.update(over)
    return client.post("/api/payroll/schedules", json=body)


def test_preview_route(client, team_repo):
    r = client.post("/api/payroll/preview", json={"repo": "me/app", "owner_login": "me", "total": 20})
    assert r.status_code == 200 and r.json()["contributors"]
    assert client.post("/api/payroll/preview", json={"repo": "me/app", "owner_login": "me",
                                                     "total": 99999}).status_code == 400


def test_schedule_needs_standing_approval_and_respects_the_cap(client, frozen_split):
    assert _create(client, approved=False).status_code == 400
    assert _create(client, total=99999).status_code == 400
    assert _create(client, pay_day=31).status_code == 422


def test_schedule_heads_up_edit_and_pay_now(client, frozen_split, fake_paypal):
    s = _create(client).json()
    run = s["current_run"]
    assert run and run["status"] == "preview" and not fake_paypal.batches  # never paid at creation
    assert client.put(f"/api/payroll/runs/{run['id']}/amounts",
                      json={"revision": 1, "amounts": {"alice": 2}}).json()["total"] == 5
    assert client.put(f"/api/payroll/runs/{run['id']}/amounts",
                      json={"revision": 1, "amounts": {"alice": 1}}).status_code == 409  # stale
    for bad in ({"zed": 1}, {"alice": -1}, {"alice": 9999}):
        assert client.put(f"/api/payroll/runs/{run['id']}/amounts",
                          json={"revision": 2, "amounts": bad}).status_code in (400, 409)
    assert client.post(f"/api/payroll/runs/{run['id']}/pay-now", json={"revision": 2}).status_code == 400
    assert client.post(f"/api/payroll/runs/{run['id']}/pay-now",
                       json={"revision": 1, "approved": True}).status_code == 409
    paid = client.post(f"/api/payroll/runs/{run['id']}/pay-now", json={"revision": 2, "approved": True}).json()
    assert paid["status"] == "paid" and paid["total"] == 5 and len(fake_paypal.batches) == 1
    view = client.get("/api/payroll/schedules").json()[0]
    assert view["current_run"] is None and view["runs"][0]["status"] == "paid"


def test_emails_apply_to_the_waiting_run(client, frozen_split, fake_paypal):
    s = _create(client).json()
    client.put(f"/api/payroll/schedules/{s['id']}/emails", json={"emails": {"@alice": "alice@real.com"}})
    run = client.get("/api/payroll/schedules").json()[0]["current_run"]
    assert next(c for c in run["result"]["contributors"] if c["login"] == "alice")["email"] == "alice@real.com"


def test_skip_pause_resume_and_tick(client, frozen_split, fake_paypal):
    s = _create(client).json()
    run_id = s["current_run"]["id"]
    assert client.post(f"/api/payroll/runs/{run_id}/skip").json()["status"] == "skipped"
    assert client.post(f"/api/payroll/runs/{run_id}/skip").status_code == 409
    assert client.post(f"/api/payroll/schedules/{s['id']}/pause").json()["active"] is False
    assert client.post(f"/api/payroll/schedules/{s['id']}/resume").json()["active"] is True
    assert client.post("/api/payroll/tick").json() == {"actions": []}
    assert not fake_paypal.batches
    assert client.post("/api/payroll/schedules/nope/pause").status_code == 404


def test_delete_schedule_removes_it_and_its_history(client, frozen_split, fake_paypal):
    s = _create(client).json()
    assert s["current_run"]
    assert client.delete(f"/api/payroll/schedules/{s['id']}").json() == {"deleted": s["id"]}
    assert client.get("/api/payroll/schedules").json() == []
    assert not db.runs_for_schedule(s["id"])
    assert client.delete(f"/api/payroll/schedules/{s['id']}").status_code == 404


def test_delete_is_refused_while_paying(client, frozen_split, fake_paypal):
    s = _create(client).json()
    db.update_run(s["current_run"]["id"], status="paying")
    assert client.delete(f"/api/payroll/schedules/{s['id']}").status_code == 409
