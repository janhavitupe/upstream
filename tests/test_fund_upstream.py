"""Fund your upstream: signals, analysis, AI review, payouts, re-queue, carry-over, report."""
import json

import pytest

from backend import config, db, requeue
from backend.agent import explain
from backend.oss import analyzer, deps, scoring, signals
from backend.paypal.client import PayPalError


# ---------- dependency files and source repos ----------

def test_pyproject_dependencies():
    text = '[project]\nname = "x"\ndependencies = ["click>=8", "rich", "Jinja2 ; python_version>\'3\'"]\n'
    assert deps.parse_pyproject(text) == ["click", "rich", "jinja2"]


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


@pytest.fixture
def registry(monkeypatch):
    signals._source_cache.clear()
    pages = {}
    monkeypatch.setattr(signals.requests, "get",
                        lambda url, timeout=None: _Resp(200, pages[url]) if url in pages else _Resp(404, {}))
    return pages


def test_find_source_repo_from_pypi_prefers_source_links(registry):
    registry["https://pypi.org/pypi/click/json"] = {"info": {
        "project_urls": {"Donate": "https://github.com/sponsors/pallets",
                         "Source": "https://github.com/pallets/click/"},
        "home_page": None}}
    assert signals.find_source_repo("click", "pypi") == ("pallets", "click")


def test_find_source_repo_from_npm(registry):
    registry["https://registry.npmjs.org/debug"] = {
        "repository": {"url": "git+https://github.com/debug-js/debug.git"}}
    assert signals.find_source_repo("debug", "npm") == ("debug-js", "debug")


def test_find_source_repo_unknown_package(registry):
    assert signals.find_source_repo("no-such-package", "pypi") is None


def test_bots_are_not_people():
    assert signals._is_bot("dependabot[bot]", "")
    assert signals._is_bot(None, "renovate-bot")
    assert not signals._is_bot("davidism", "David Lord")


def test_funding_platforms_ignore_empty_and_commented_lines():
    text = "github: pallets\ncustom: https://x\nko_fi: []\nopen_collective: # none\npatreon:\n"
    assert signals.funding_platforms(text) == ["github", "custom"]


def _repo_signals_fixture(fake_github, *, org_funding=True):
    fake_github.api["/repos/acme/lib"] = {"owner": {"login": "acme"}, "name": "lib",
                                          "html_url": "https://github.com/acme/lib",
                                          "stargazers_count": 10, "archived": False,
                                          "open_issues_count": 4, "pushed_at": "2026-10-01T00:00:00Z"}
    commit = lambda login, name="", msg="fix: thing", parents=1: {  # noqa: E731
        "author": {"login": login} if login else None,
        "commit": {"author": {"name": name, "email": f"{name}@x"}, "message": msg},
        "parents": [{}] * parents}
    fake_github.api["/repos/acme/lib/commits"] = [
        commit("alice", msg="feat: add retries\n\nlong body"), commit("alice"), commit("bob"),
        commit("dependabot[bot]", msg="bump deps")]
    fake_github.api["/repos/acme/lib/releases"] = [
        {"published_at": "2026-09-01T00:00:00Z", "draft": False, "prerelease": False},
        {"published_at": "2026-09-10T00:00:00Z", "draft": False, "prerelease": True},
        {"published_at": "2019-01-01T00:00:00Z", "draft": False, "prerelease": False}]
    fake_github.api["/repos/acme/lib/languages"] = {"Python": 40960, "C": 10240}
    if org_funding:
        fake_github.files[("acme", ".github", "FUNDING.yml")] = "github: acme\n"


def test_repo_signals_count_people_releases_size_and_org_funding(fake_github):
    _repo_signals_fixture(fake_github)
    s = signals.repo_signals("acme", "lib")
    assert s["human_commits_last_year"] == 3 and s["commits_last_year"] == 4  # bot excluded
    assert s["active_maintainers"] == 2
    assert s["releases_last_year"] == 2 and s["last_release"] == "2026-09-01T00:00:00Z"
    assert s["code_kb"] == 50.0
    assert s["has_funding"] and s["funding_source"] == "org" and s["funding_platforms"] == ["github"]
    assert s["top_committers"][0] == {"login": "alice", "commits": 2}
    assert s["recent_commit_subjects"][0] == "feat: add retries"


def test_repo_signals_without_funding(fake_github):
    _repo_signals_fixture(fake_github, org_funding=False)
    s = signals.repo_signals("acme", "lib")
    assert not s["has_funding"] and s["funding_platforms"] == []


def test_health_risk_reasons():
    risk, reasons = scoring.health_risk({"active_maintainers": 1, "last_release": "2020-01-01T00:00:00Z",
                                         "archived": True, "has_funding": False, "security_issues": 2})
    assert risk == 1.0 or risk > 0.8
    assert any("solo maintainer" in r for r in reasons) and any("archived" in r for r in reasons)


# ---------- the full analysis ----------

@pytest.fixture
def express_like(fake_github, monkeypatch):
    fake_github.api["/repos/me/app"] = {"owner": {"login": "me"}, "name": "app",
                                        "default_branch": "main"}
    fake_github.files[("me", "app", "package.json")] = json.dumps(
        {"dependencies": {"busy": "1", "small": "1", "idle": "1", "ghost": "1"}})
    fake_github.trees[("me", "app")] = ["lib/a.js", "lib/b.js", "test/t.js"]
    fake_github.files[("me", "app", "lib/a.js")] = "const busy = require('busy'); require('idle')"
    fake_github.files[("me", "app", "lib/b.js")] = "import small from 'small'; require('busy')"
    sig = lambda commits, rels, kb, who: {  # noqa: E731
        "html_url": f"https://github.com/x/{who}", "human_commits_last_year": commits,
        "commits_last_year": commits, "releases_last_year": rels, "code_kb": kb,
        "active_maintainers": 3, "has_funding": True, "funding_platforms": [],
        "top_committers": [{"login": f"{who}-dev", "commits": commits}] if commits else [],
        "recent_commit_subjects": ["fix: x"]}
    table = {"busy": sig(80, 6, 120, "busy"), "small": sig(5, 0, 15, "small"), "idle": sig(0, 0, 40, "idle")}
    monkeypatch.setattr(signals, "find_source_repo",
                        lambda pkg, eco: None if pkg == "ghost" else ("x", pkg))
    monkeypatch.setattr(signals, "repo_signals", lambda o, r: table[r])


def test_analysis_without_ai_uses_rule_reasons(express_like):
    r = analyzer.analyze_repo("me/app", 100, 10)
    by = {d["package"]: d for d in r["dependencies"]}
    assert r["explained_by"] == "rules" and r["ecosystem"] == "npm"
    assert by["busy"]["import_count"] == 2 and by["small"]["import_count"] == 1
    assert by["idle"]["amount"] == 0 and by["ghost"]["amount"] == 0 and "error" in by["ghost"]
    assert round(sum(d["amount"] for d in r["dependencies"]), 2) == 100
    assert by["busy"]["intended_for"]["login"] == "busy-dev"
    assert by["busy"]["suggested_email"] in config.DEMO_MAINTAINER_EMAILS
    assert any("not funded: idle" in w for w in r["warnings"])


def test_analysis_with_ai_review_is_bounded(express_like, fake_llm):
    fake_llm.reply(json.dumps({
        "busy": {"explanation": "Lots of feature work.", "adjust_points": -30, "adjust_reason": "x"},
        "small": {"explanation": "A security fix.", "adjust_points": 4, "adjust_reason": "security fix"}}))
    r = analyzer.analyze_repo("me/app", 100, 10)
    by = {d["package"]: d for d in r["dependencies"]}
    assert r["explained_by"] == "llm" and r["ai_adjust_limit_points"] == explain.MAX_ADJUST_POINTS
    assert by["busy"]["ai_adjust_points"] == -5          # asked -30, clamped
    assert by["small"]["explanation"] == "A security fix."
    assert round(sum(d["amount"] for d in r["dependencies"]), 2) == 100
    prompt = fake_llm.prompt()
    assert "recent_commit_subjects" in prompt and "idle" not in prompt  # idle isn't sent to the AI


def test_ai_review_returning_nonsense_falls_back_to_rules(express_like, fake_llm):
    fake_llm.reply("I think everything is great!")
    r = analyzer.analyze_repo("me/app", 100, 10)
    assert r["explained_by"] == "rules"


def test_analysis_errors(fake_github):
    fake_github.api["/repos/me/empty"] = {"owner": {"login": "me"}, "name": "empty", "default_branch": "main"}
    with pytest.raises(ValueError, match="No dependencies"):
        analyzer.analyze_repo("me/empty", 10)
    with pytest.raises(ValueError):
        analyzer.analyze_repo("not a repo", 10)


def test_analyze_api_saves_and_lists(client, express_like):
    r = client.post("/api/oss/analyze", json={"repo": "me/app", "budget": 50, "max_deps": 5}).json()
    assert r["analysis_id"] and r["revision"] == 1
    assert client.get("/api/oss/analyses").json()[0]["analysis_id"] == r["analysis_id"]
    got = client.get(f"/api/oss/analyses/{r['analysis_id']}").json()
    assert got["repo"] == "me/app" and got["payout"] is None
    assert client.get("/api/oss/analyses/nope").status_code == 404


# ---------- payouts, status, re-queue, carry-over ----------

def _save(amounts, emails=None):
    deps_ = [{"package": p, "amount": a, "percent": 0, "signals": {"x": 1},
              "suggested_email": (emails or {}).get(p, f"{p}@example.com"),
              "intended_for": {"login": f"{p}-dev", "commits": 3}, "explanation": f"why {p}"}
             for p, a in amounts.items()]
    return db.save_analysis({"repo": "o/r", "budget": sum(amounts.values()), "dependencies": deps_,
                             "warnings": [], "explained_by": "rules", "revision": 1, "history": [],
                             "sandbox": True})


def _pay(client, aid, rev=1, **extra):
    return client.post("/api/oss/payout", json={"analysis_id": aid, "revision": rev, "approved": True, **extra})


def test_payout_note_names_who_it_is_for(client, fake_paypal):
    aid = _save({"a": 3})
    assert _pay(client, aid).status_code == 200
    note = next(iter(fake_paypal.items.values()))["payout_item"]["note"]
    assert "@a-dev" in note and "sandbox stand-in" in note and "why a" in note


def test_missing_email_is_refused(client, fake_paypal):
    aid = _save({"a": 3}, emails={"a": ""})
    r = _pay(client, aid)
    assert r.status_code == 400 and "Missing PayPal email" in r.json()["detail"] and not fake_paypal.batches
    assert _pay(client, aid, emails={"a": "a@x.com"}).status_code == 200  # override fixes it


def test_status_refresh_updates_each_person(client, fake_paypal):
    aid = _save({"a": 3, "b": 2})
    batch = _pay(client, aid).json()["batch"]["payout_batch_id"]
    s = client.get(f"/api/oss/payout/{batch}").json()
    assert s["status"] == "SUCCESS" and {i["status"] for i in s["items"]} == {"SUCCESS"}
    assert all(i["payout_item_id"] for i in s["items"])


def test_paypal_error_releases_carryover_and_pot(client, fake_paypal):
    db.add_pot(5, "test", "INV-X")
    aid = _save({"a": 5})
    a = db.get_analysis(aid)
    a["pot"] = {"ids": [p["id"] for p in db.open_pot()], "amount": 5, "sources": []}
    db.update_analysis(aid, a)
    fake_paypal.fail_next["/v1/payments/payouts"] = PayPalError(500, {"name": "INTERNAL"})
    assert _pay(client, aid).status_code == 500
    assert db.open_pot(), "pot must be released after a failed payout"
    assert _pay(client, aid).status_code == 200 and not db.open_pot()


def test_unclaimed_payout_is_requeued_into_the_next_analysis(client, fake_paypal, express_like):
    fake_paypal.unclaimed_emails.add("ghost@x.com")
    aid = _save({"busy": 4}, emails={"busy": "ghost@x.com"})
    batch = _pay(client, aid).json()["batch"]["payout_batch_id"]
    client.get(f"/api/oss/payout/{batch}")  # refresh: UNCLAIMED, inside the grace period
    assert db.open_carryover() == []
    r = client.post(f"/api/oss/payout/{batch}/requeue").json()
    assert r["requeued"] == [{"package": "busy", "amount": 4}] and r["failed"] == []
    assert client.post(f"/api/oss/payout/{batch}/requeue").json()["requeued"] == []  # once
    nxt = client.post("/api/oss/analyze", json={"repo": "me/app", "budget": 10}).json()
    busy = next(d for d in nxt["dependencies"] if d["package"] == "busy")
    assert busy["carryover"] == 4 and any("re-queued" in w for w in nxt["warnings"])


def test_requeue_reports_paypal_refusals(fake_paypal):
    fake_paypal.unclaimed_emails.add("u@x.com")
    resp = fake_paypal.request("POST", "/v1/payments/payouts", json={
        "sender_batch_header": {"sender_batch_id": "s1"},
        "items": [{"receiver": "u@x.com", "amount": {"value": "2.00"}, "sender_item_id": "pkg"}]})
    batch = resp["batch_header"]["payout_batch_id"]
    item = next(iter(fake_paypal.items))
    db.save_batch(batch, None, "s1", "PENDING", [{"item_id": "pkg", "email": "u@x.com", "amount": 2}])
    db.update_item(batch, "pkg", status="UNCLAIMED", payout_item_id=item)
    fake_paypal.fail_next[f"/payouts-item/{item}/cancel"] = PayPalError(500, {"name": "INTERNAL_SERVICE_ERROR"})
    requeued, failed = requeue.requeue(batch, 0)
    assert requeued == [] and "INTERNAL_SERVICE_ERROR" in failed[0]["error"]
    assert db.get_batch(batch)["items"][0]["status"] == "UNCLAIMED"  # retried next time


def test_returned_webhook_requeues_once(client):
    db.save_batch("B1", None, "s1", "SUCCESS", [{"item_id": "pkg", "email": "e@x.com", "amount": 3}])
    hook = {"event_type": "PAYMENT.PAYOUTS-ITEM.RETURNED",
            "resource": {"id": "IT1", "payout_batch_id": "B1", "payout_item_id": "IT1",
                         "transaction_status": "RETURNED", "payout_item": {"sender_item_id": "pkg"}}}
    client.post("/webhooks/paypal", json=hook)
    client.post("/webhooks/paypal", json=hook)
    assert [(c["package"], c["amount"]) for c in db.open_carryover()] == [("pkg", 3)]


def test_succeeded_webhook_updates_the_item(client):
    db.save_batch("B2", None, "s2", "PENDING", [{"item_id": "pkg", "email": "e@x.com", "amount": 3}])
    client.post("/webhooks/paypal", json={"event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "resource": {
        "id": "IT2", "payout_batch_id": "B2", "payout_item_id": "IT2", "transaction_status": "SUCCESS",
        "payout_item": {"sender_item_id": "pkg"}}})
    assert db.get_batch("B2")["items"][0]["status"] == "SUCCESS"


def test_pot_funds_an_analysis(client, express_like):
    db.add_pot(6, "from Acme", "INV-1")
    db.add_pot(4, "from Globex", "INV-2")
    r = client.post("/api/oss/analyze", json={"repo": "me/app", "budget": 999, "use_pot": True}).json()
    assert r["budget"] == 10 and r["pot"]["amount"] == 10
    assert client.post("/api/oss/analyze", json={"repo": "me/app", "budget": 5, "use_pot": True}).status_code == 200
    db.claim_pot(r["pot"]["ids"], r["analysis_id"])
    assert client.post("/api/oss/analyze", json={"repo": "me/app", "budget": 5, "use_pot": True}).status_code == 400


def test_giving_report_is_public_without_emails(client, fake_paypal):
    aid = _save({"a": 3, "b": 2})
    _pay(client, aid)
    html = client.get(f"/report/{aid}").text
    assert "funded 2 open-source projects" in html and "@a-dev" in html
    assert "example.com" not in html and "Paid" in html
    assert client.get("/report/nope").status_code == 404
