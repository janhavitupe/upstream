"""Team pay status from PayPal, and webhooks that only change state when trusted."""
from backend import db, payroll
from tests.test_team_payroll import _run


def test_batch_status_shows_who_has_been_paid(client, fake_paypal):
    fake_paypal.unclaimed_emails.add("b@x.com")
    _run([{"login": "alice", "amount": 6, "email": "a@x.com", "explanation": "x"},
          {"login": "bob", "amount": 4, "email": "b@x.com", "explanation": "y"}])
    batch_id = payroll.pay_run("R1")["batch_id"]
    b = client.get(f"/api/payroll/batches/{batch_id}").json()
    status = {i["sender_item_id"]: i["status"] for i in b["items"]}
    assert status == {"alice": "SUCCESS", "bob": "UNCLAIMED"} and b["status"] == "SUCCESS"
    assert client.get("/api/payroll/batches/NOPE").status_code == 404


def test_payouts_go_out_with_team_wording(fake_paypal):
    _run([{"login": "alice", "amount": 6, "email": "a@x.com", "explanation": "x"}])
    batch = fake_paypal.batches[payroll.pay_run("R1")["batch_id"]]
    assert "open source" not in batch["subject"].lower()


def test_unverified_webhooks_are_logged_but_change_nothing_on_a_public_deploy(client, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, "IS_PUBLIC_DEPLOY", True)
    db.save_batch("B9", None, "s9", "PENDING", [{"item_id": "a", "email": "a@x.com", "amount": 1}])
    hook = {"event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
            "resource": {"batch_header": {"payout_batch_id": "B9", "batch_status": "SUCCESS"}}}
    assert client.post("/webhooks/paypal", json=hook).status_code == 200
    assert db.get_batch("B9")["status"] == "PENDING"
    assert client.get("/api/events").json()[0]["event_type"] == "PAYMENT.PAYOUTSBATCH.SUCCESS"
