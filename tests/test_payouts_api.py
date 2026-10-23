"""Money out waits for your yes: approval, exact amounts, caps, no double payment."""
import pytest

from backend import db
from backend.paypal import payouts


def _analysis(amounts, budget=None):
    deps = [{"package": p, "amount": a, "percent": 0.0, "signals": {"x": 1},
             "suggested_email": f"{p}@example.com", "explanation": f"why {p}"}
            for p, a in amounts.items()]
    result = {"repo": "o/r", "budget": budget or sum(amounts.values()), "dependencies": deps,
              "warnings": [], "explained_by": "rules", "revision": 1, "history": []}
    return db.save_analysis(result)


@pytest.fixture
def paypal_calls(monkeypatch):
    calls = []

    def fake_create_batch(items, **kw):
        calls.append((items, kw))
        return {"batch_header": {"payout_batch_id": f"B{len(calls)}", "batch_status": "PENDING"}}
    monkeypatch.setattr(payouts, "create_batch", fake_create_batch)
    return calls


def test_payout_needs_explicit_approval(client, paypal_calls):
    aid = _analysis({"a": 10, "b": 5})
    r = client.post("/api/oss/payout", json={"analysis_id": aid, "revision": 1})
    assert r.status_code == 400 and not paypal_calls


def test_payout_pays_exactly_the_saved_split_once(client, paypal_calls):
    aid = _analysis({"a": 10, "b": 5})
    r = client.post("/api/oss/payout", json={"analysis_id": aid, "revision": 1, "approved": True})
    assert r.status_code == 200 and r.json()["total"] == 15
    items = paypal_calls[0][0]
    assert sorted((i["item_id"], i["amount"]) for i in items) == [("a", 10), ("b", 5)]
    assert paypal_calls[0][1]["sender_batch_id"] == f"upstream-{aid}"  # PayPal-side idempotency
    again = client.post("/api/oss/payout", json={"analysis_id": aid, "revision": 1, "approved": True})
    assert again.status_code == 409 and len(paypal_calls) == 1


def test_editing_creates_a_new_revision_and_old_ones_cannot_be_paid(client, paypal_calls):
    aid = _analysis({"a": 10, "b": 5})
    r = client.put(f"/api/oss/analyses/{aid}/allocation", json={"revision": 1, "amounts": {"a": 2}})
    assert r.status_code == 200 and r.json()["revision"] == 2
    stale = client.post("/api/oss/payout", json={"analysis_id": aid, "revision": 1, "approved": True})
    assert stale.status_code == 409 and not paypal_calls
    ok = client.post("/api/oss/payout", json={"analysis_id": aid, "revision": 2, "approved": True})
    assert ok.json()["total"] == 7


@pytest.mark.parametrize("amounts,status", [({"a": -1}, 400), ({"nope": 1}, 400), ({"a": 10_000}, 400)])
def test_bad_edits_are_refused(client, amounts, status):
    aid = _analysis({"a": 10, "b": 5})
    r = client.put(f"/api/oss/analyses/{aid}/allocation", json={"revision": 1, "amounts": amounts})
    assert r.status_code == status


def test_monthly_cap_is_enforced(client, paypal_calls):
    aid = _analysis({"a": 400, "b": 200})  # over the 500 cap set in conftest
    r = client.post("/api/oss/payout", json={"analysis_id": aid, "revision": 1, "approved": True})
    assert r.status_code == 400 and "cap" in r.json()["detail"] and not paypal_calls


def test_pot_can_only_be_spent_once():
    assert db.add_pot(6, "test", "INV-1")
    assert not db.add_pot(6, "test again", "INV-1")  # one give-back per payment
    ids = [p["id"] for p in db.open_pot()]
    assert db.claim_pot(ids, "analysis-1")
    assert not db.claim_pot(ids, "analysis-2")
    db.release_pot("analysis-1")
    assert db.claim_pot(ids, "analysis-2")


def test_unverified_webhooks_are_logged_but_change_nothing_on_a_public_deploy(client, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "IS_PUBLIC_DEPLOY", True)
    db.save_batch("B9", None, "s9", "PENDING", [{"item_id": "a", "email": "a@x.com", "amount": 1}])
    hook = {"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
            "resource": {"batch_header": {"payout_batch_id": "B9", "batch_status": "SUCCESS"}}}
    assert client.post("/webhooks/paypal", json=hook).status_code == 200
    assert db.get_batch("B9")["status"] == "PENDING"
    assert client.get("/api/events").json()[0]["event_type"] == "PAYMENT.PAYOUTSBATCH.SUCCESS"
