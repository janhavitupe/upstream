"""Unclaimed payouts: get the money back and re-queue it for that package's next payout.

Two ways money comes back, both handled here:
- RETURNED: PayPal returned an unclaimed item on its own (after 30 days in live mode).
- UNCLAIMED past the grace period: Upstream cancels it, and PayPal returns the funds.
Money coming back needs no approval. Re-queuing is idempotent per item.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend import db
from backend.paypal import payouts
from backend.paypal.client import PayPalError


def requeue(batch_id: str, min_age_days: float) -> tuple[list[dict], list[dict]]:
    """Returns (requeued, failed) as lists of {"package", "amount"} / {"package", "error"}."""
    stored = db.get_batch(batch_id)
    if not stored:
        return [], []
    age = datetime.now(timezone.utc) - datetime.fromisoformat(stored["created_at"])
    old_enough = age.total_seconds() >= min_age_days * 86400
    requeued, failed = [], []
    for item in stored["items"]:
        pkg, status = item["sender_item_id"], item["status"]
        if status == "UNCLAIMED" and old_enough and item["payout_item_id"]:
            try:
                resp = payouts.cancel_unclaimed_item(item["payout_item_id"])
            except PayPalError as e:  # stays UNCLAIMED; the next refresh tries again
                body = e.body if isinstance(e.body, dict) else {}
                failed.append({"package": pkg,
                               "error": f"PayPal {e.status}: {body.get('name') or e.body}"})
                continue
            status = resp.get("transaction_status") or "RETURNED"
            db.update_item(batch_id, pkg, status=status)
        if status == "RETURNED" and db.add_carryover(pkg, item["amount"], batch_id, pkg):
            requeued.append({"package": pkg, "amount": item["amount"]})
    return requeued, failed
