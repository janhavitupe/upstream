"""Monthly team payroll: pay the top contributors of a repo you own, on a set day.

Same principle as dependency funding, per person: pay for work done (commits) times its size
(lines changed), with diminishing returns; the AI reads each person's commit messages and
adjusts by at most a few points. The owner approves the schedule once. Before each payday a
preview is made (the heads-up); the owner can skip or edit it, and if they do nothing it is
paid automatically, never before payday and never before the heads-up has run its course.
"""
from __future__ import annotations

import calendar
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone

from backend import config, db
from backend.agent.explain import MAX_ADJUST_POINTS, review_contributors
from backend.oss import github, scoring
from backend.oss.signals import _is_bot
from backend.paypal import payouts
from backend.paypal.client import PayPalError

COMMIT_PAGES = 5  # up to 500 commits per period
SUBJECTS_PER_PERSON = 12
_tick_lock = threading.Lock()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ---------- who did what ----------

def contributor_work(owner: str, repo: str, since: datetime, until: datetime,
                     exclude: set[str]) -> tuple[dict[str, dict], list[str]]:
    """{login: {"commits", "lines_changed", "subjects"}} for human, non-merge commits."""
    warnings: list[str] = []
    people: dict[str, dict] = {}
    unlinked = 0
    params = {"since": _iso(since), "until": _iso(until), "per_page": 100}
    for page in range(1, COMMIT_PAGES + 1):
        commits = github.api_get(f"/repos/{owner}/{repo}/commits", {**params, "page": page})
        for c in commits:
            login = (c.get("author") or {}).get("login")
            name = (c.get("commit", {}).get("author") or {}).get("name") or ""
            if _is_bot(login, name) or len(c.get("parents") or []) > 1:
                continue  # bots and merge commits aren't anyone's work
            if not login:
                unlinked += 1  # no GitHub account linked: can't be identified or paid
                continue
            if login.lower() in exclude:
                continue
            p = people.setdefault(login, {"commits": 0, "lines_changed": None, "subjects": []})
            p["commits"] += 1
            subject = ((c.get("commit", {}).get("message") or "").strip().splitlines() or [""])[0]
            if subject and len(p["subjects"]) < SUBJECTS_PER_PERSON:
                p["subjects"].append(subject[:120])
        if len(commits) < 100:
            break
    else:
        warnings.append(f"Counted the first {COMMIT_PAGES * 100} commits of this period.")
    if unlinked:
        warnings.append(f"{unlinked} commit(s) had no linked GitHub account and weren't counted.")

    stats = github.contributor_stats(owner, repo)
    if stats is None:
        warnings.append("GitHub hasn't computed line counts for this repo yet, so the split "
                        "uses commits only. Try again in a few minutes.")
    else:
        # Weekly buckets: count weeks that start inside the period (or in the week before it)
        lo, hi = since.timestamp() - 7 * 86400, until.timestamp()
        for s in stats:
            login = (s.get("author") or {}).get("login")
            if login in people:
                people[login]["lines_changed"] = sum(
                    w.get("a", 0) + w.get("d", 0) for w in s.get("weeks", []) if lo <= w["w"] < hi)
    return people, warnings


def compute_split(repo_ref: str, owner_login: str, total: float, top_n: int,
                  since: datetime, until: datetime, emails: dict | None = None) -> dict:
    owner, name = github.parse_repo_ref(repo_ref)
    info = github.repo_info(owner, name)
    owner, name = info["owner"]["login"], info["name"]
    exclude = {owner_login.lower().lstrip("@")}
    people, warnings = contributor_work(owner, name, since, until, exclude)

    rows = []
    for login, p in people.items():
        lines = p["lines_changed"]
        weight = (p["commits"] * max(lines, 1)) ** 0.5 if lines is not None else p["commits"]
        reasons = [f"{p['commits']} commit{'s' if p['commits'] != 1 else ''}"
                   + (f", {lines:,} lines changed" if lines is not None else "")]
        rows.append({"login": login, **p, "weight": weight, "reasons": reasons})
    rows.sort(key=lambda r: -r["weight"])
    if len(rows) > top_n:
        warnings.append(f"Paying the top {top_n} of {len(rows)} contributors this period.")
    paid, rest = rows[:top_n], rows[top_n:]

    total_w = sum(r["weight"] for r in paid)
    for r, amount in zip(paid, scoring.split_cents([r["weight"] for r in paid], total)):
        r["amount"] = amount
        r["percent"] = round(100 * r["weight"] / total_w, 1) if total_w else 0.0
    review = review_contributors(paid, total) if paid else None
    if review:
        paid = scoring.apply_ai_adjustments(paid, total, review, MAX_ADJUST_POINTS, key="login")

    demo = config.DEMO_MAINTAINER_EMAILS
    sandbox = "sandbox" in config.PAYPAL_BASE
    for i, r in enumerate(paid):
        r.pop("weight", None)
        r["explanation"] = ((review or {}).get(r["login"], {}).get("explanation")
                            or "; ".join(r["reasons"]).capitalize())
        r["email"] = (emails or {}).get(r["login"]) or (demo[i % len(demo)] if sandbox and demo
                                                         else "")
    if not paid:
        warnings.append("No contributors (other than you) with linked GitHub accounts in this "
                        "period, so nothing would be paid.")
    return {
        "repo": f"{owner}/{name}", "owner_login": owner_login.lstrip("@"),
        "window_start": _iso(since), "window_end": _iso(until), "total": total,
        "contributors": paid, "others": [{"login": r["login"], "commits": r["commits"]}
                                         for r in rest],
        "explained_by": "llm" if review else "rules",
        "ai_adjust_limit_points": MAX_ADJUST_POINTS if review else 0,
        "sandbox": sandbox, "warnings": warnings,
    }


# ---------- the monthly schedule ----------

def pay_date(year: int, month: int, pay_day: int) -> date:
    return date(year, month, min(pay_day, calendar.monthrange(year, month)[1]))


def due_period(schedule: dict, now: datetime) -> tuple[str, date] | None:
    """This month's payday, unless the schedule started after it."""
    pd = pay_date(now.year, now.month, schedule["pay_day"])
    if pd < _parse(schedule["created_at"]).date():
        return None
    return f"{pd:%Y-%m}", pd


def next_payday(schedule: dict, now: datetime) -> date:
    due = due_period(schedule, now)
    if due:
        run = db.run_for_period(schedule["id"], due[0])
        if not run or run["status"] == "preview":
            return due[1]
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return pay_date(y, m, schedule["pay_day"])


def make_preview(schedule: dict, period: str, payday: date, now: datetime) -> dict | None:
    since = (_parse(schedule["last_window_end"]) if schedule.get("last_window_end")
             else now - timedelta(days=30))
    split = compute_split(schedule["repo"], schedule["owner_login"], schedule["total"],
                          schedule["top_n"], since, now, schedule["emails"])
    payday_start = datetime.combine(payday, time(0, 0), tzinfo=timezone.utc)
    pay_after = max(payday_start, now + timedelta(days=schedule["notice_days"]))
    run = {"id": uuid.uuid4().hex[:12], "schedule_id": schedule["id"], "period": period,
           "status": "preview", "pay_after": _iso(pay_after), "window_start": split["window_start"],
           "window_end": split["window_end"], "result": split}
    if not db.create_run(run):
        return None  # another tick made it first
    total = sum(r["amount"] for r in split["contributors"])
    db.save_event({"event_type": "UPSTREAM.PAYROLL.PREVIEW", "resource": {"id": run["id"]}},
                  message=f"Heads-up: {split['repo']} payroll of ${total:.2f} to "
                          f"{len(split['contributors'])} contributor(s) pays on {pay_after:%b %d}. "
                          "Skip or edit it before then.", verified=None)
    return db.get_run(run["id"])


def pay_run(run_id: str) -> dict:
    """Pay a preview run. Raises ValueError (not payable) or PayPalError."""
    run = db.get_run(run_id)
    if not run:
        raise ValueError("Payroll run not found.")
    if run["status"] != "preview":
        raise ValueError(f"This run is already {run['status']}.")
    schedule = db.get_schedule(run["schedule_id"])
    lines = [r for r in run["result"]["contributors"] if r.get("amount", 0) > 0]
    total = round(sum(r["amount"] for r in lines), 2)
    if not lines:
        db.update_run(run_id, expect_status="preview", status="skipped", note="Nothing to pay")
        db.update_schedule(schedule["id"], last_window_end=run["window_end"])
        raise ValueError("Nothing to pay in this run.")
    if total > config.PAYROLL_MONTHLY_CAP:
        raise ValueError(f"Total ${total:.2f} exceeds the payroll cap of "
                         f"${config.PAYROLL_MONTHLY_CAP:.2f} (PAYROLL_MONTHLY_CAP).")
    missing = [r["login"] for r in lines if "@" not in (r.get("email") or "")]
    if missing:
        raise ValueError(f"Add a PayPal email for: {', '.join(missing)}")
    sandbox = run["result"].get("sandbox")
    items = [{"email": r["email"], "amount": r["amount"], "item_id": r["login"][:63],
              "note": (f"Upstream payroll for @{r['login']}'s work on {run['result']['repo']}, "
                       f"{run['period']}" + (" (sandbox stand-in)" if sandbox else "")
                       + f". {r.get('explanation') or ''}").strip()}
             for r in lines]
    # Claim the run before paying, so two ticks can't both pay it; PayPal's sender_batch_id
    # rejects a repeat as a second guard
    if not db.update_run(run_id, expect_status="preview", status="paying"):
        raise ValueError("This run is already being paid.")
    sender_batch_id = f"payroll-{run_id}"
    try:
        resp = payouts.create_batch(items, sender_batch_id=sender_batch_id,
                                    subject=f"Your {run['period']} pay for {run['result']['repo']}",
                                    message="Paid by the repo owner through Upstream.")
    except PayPalError as e:  # back to waiting, so the next tick (or Pay now) retries
        db.update_run(run_id, status="preview",
                      note=f"Last payment attempt failed: PayPal {e.status}: {str(e.body)[:300]}")
        raise
    header = resp.get("batch_header", {})
    db.save_batch(header["payout_batch_id"], None, sender_batch_id, header.get("batch_status"),
                  items)
    db.update_run(run_id, status="paid", batch_id=header["payout_batch_id"], paid_at=db.now())
    db.update_schedule(schedule["id"], last_window_end=run["window_end"])
    db.save_event({"event_type": "UPSTREAM.PAYROLL.PAID", "resource": {"id": run_id}},
                  message=f"Paid ${total:.2f} payroll for {run['result']['repo']} "
                          f"({run['period']}) to {len(items)} contributor(s).", verified=None)
    return db.get_run(run_id)


def skip_run(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run or not db.update_run(run_id, expect_status="preview", status="skipped",
                                    note="Skipped by the owner"):
        raise ValueError("Only a run waiting for payday can be skipped.")
    db.update_schedule(run["schedule_id"], last_window_end=run["window_end"])
    return db.get_run(run_id)


def tick(now: datetime | None = None, pay: bool = True) -> list[str]:
    """Make due previews and pay due runs. Safe to call often and from several places.

    pay=False only makes previews, so a newly created schedule is never paid in the same
    moment it is set up: the owner always sees the split first.
    """
    now = now or datetime.now(timezone.utc)
    actions = []
    with _tick_lock:
        for s in db.list_schedules(active_only=True):
            due = due_period(s, now)
            if not due:
                continue
            period, payday = due
            run = db.run_for_period(s["id"], period)
            heads_up_from = datetime.combine(payday - timedelta(days=s["notice_days"]), time(0, 0),
                                             tzinfo=timezone.utc)
            try:
                if not run and now >= heads_up_from:
                    run = make_preview(s, period, payday, now)
                    if run:
                        actions.append(f"preview {s['repo']} {period}")
                if pay and run and run["status"] == "preview" and now >= _parse(run["pay_after"]):
                    pay_run(run["id"])
                    actions.append(f"paid {s['repo']} {period}")
            except (ValueError, PayPalError, github.GitHubError) as e:
                actions.append(f"error {s['repo']} {period}: {e}")
                print(f"[payroll] {s['repo']} {period}: {e}")
    return actions


def start_scheduler(interval_seconds: int = 900) -> threading.Event:
    """Background timer for while the app is awake; the daily GitHub Action covers sleep."""
    stop = threading.Event()

    def loop():
        while not stop.wait(5 if not loop.started else interval_seconds):
            loop.started = True
            try:
                tick()
            except Exception as e:  # never let the timer die
                print(f"[payroll] tick failed: {e}")
    loop.started = False
    threading.Thread(target=loop, name="payroll-scheduler", daemon=True).start()
    return stop
