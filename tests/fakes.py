"""In-memory stand-ins for PayPal, the AI and GitHub, so every flow can be tested offline.

FakePayPal behaves like the sandbox for the calls Upstream makes: it stores invoices, payouts and
subscriptions, lets tests act as the client (pay, approve), and refuses what PayPal refuses
(cancelling a paid invoice, reusing a sender_batch_id, cancelling a claimed payout).
"""
from __future__ import annotations

import itertools
import json
import re

from backend.paypal.client import PayPalError


class FakePayPal:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.invoices: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.subs: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.reminders: list[tuple[str, dict]] = []
        self.unclaimed_emails: set[str] = set()   # payouts to these stay UNCLAIMED
        self.fail_next: dict[str, PayPalError] = {}  # path substring -> error to raise once
        self._ids = itertools.count(1)

    # ---------- dispatcher (replaces paypal.request) ----------

    def request(self, method, path, *, json=None, params=None, headers=None):
        self.calls.append((method, path, json))
        for needle, err in list(self.fail_next.items()):
            if needle in path:
                del self.fail_next[needle]
                raise err
        for pattern, handler in self.routes():
            m = re.fullmatch(pattern, f"{method} {path}")
            if m:
                return handler(*m.groups(), body=json or {}, params=params or {})
        raise AssertionError(f"FakePayPal has no route for {method} {path}")

    def routes(self):
        return [
            (r"POST /v2/invoicing/invoices", self._create_invoice),
            (r"POST /v2/invoicing/invoices/([^/]+)/send", self._send_invoice),
            (r"GET /v2/invoicing/invoices/([^/]+)", lambda i, **k: self._inv(i)),
            (r"PUT /v2/invoicing/invoices/([^/]+)", self._update_invoice),
            (r"POST /v2/invoicing/invoices/([^/]+)/remind", self._remind),
            (r"POST /v2/invoicing/invoices/([^/]+)/cancel", self._cancel_invoice),
            (r"POST /v2/invoicing/invoices/([^/]+)/payments", self._record_payment),
            (r"POST /v2/invoicing/search-invoices", self._search),
            (r"POST /v1/payments/payouts", self._create_batch),
            (r"GET /v1/payments/payouts/([^/]+)", self._get_batch),
            (r"GET /v1/payments/payouts-item/([^/]+)", lambda i, **k: self._item_view(i)),
            (r"POST /v1/payments/payouts-item/([^/]+)/cancel", self._cancel_item),
            (r"POST /v1/catalogs/products", lambda **k: {"id": self._id("PROD")}),
            (r"POST /v1/billing/plans", lambda **k: {"id": self._id("P"), "status": "ACTIVE"}),
            (r"POST /v1/billing/subscriptions", self._create_sub),
            (r"GET /v1/billing/subscriptions/([^/]+)", lambda i, **k: self.subs[i]),
            (r"POST /v1/billing/subscriptions/([^/]+)/(suspend|activate|cancel)", self._sub_action),
            (r"POST /v2/checkout/orders", self._create_order),
            (r"POST /v2/checkout/orders/([^/]+)/capture", self._capture_order),
            (r"POST /v1/notifications/verify-webhook-signature",
             lambda **k: {"verification_status": "SUCCESS"}),
        ]

    def _id(self, prefix):
        return f"{prefix}-{next(self._ids):04d}"

    # ---------- invoices ----------

    def _create_invoice(self, body, **_):
        inv_id = self._id("INV2")
        total = round(sum(float(i["quantity"]) * float(i["unit_amount"]["value"])
                          for i in body["items"]), 2)
        cur = body["detail"]["currency_code"]
        self.invoices[inv_id] = {
            "id": inv_id, "status": "DRAFT", "items": body["items"],
            "primary_recipients": body["primary_recipients"], "invoicer": body.get("invoicer", {}),
            "configuration": {},
            "detail": {**body["detail"], "invoice_number": f"{len(self.invoices) + 1:04d}",
                       "viewed_by_recipient": False,
                       "metadata": {"recipient_view_url": f"https://pay.example/{inv_id}"}},
            "amount": {"currency_code": cur, "value": f"{total:.2f}"},
            "due_amount": {"currency_code": cur, "value": f"{total:.2f}"},
        }
        return {"href": f"https://api/v2/invoicing/invoices/{inv_id}"}

    def _inv(self, inv_id):
        if inv_id not in self.invoices:
            raise PayPalError(404, {"name": "RESOURCE_NOT_FOUND"})
        return json.loads(json.dumps(self.invoices[inv_id]))

    def _send_invoice(self, inv_id, **_):
        self.invoices[inv_id]["status"] = "SENT"
        return {}

    def _update_invoice(self, inv_id, body, **_):
        self.invoices[inv_id]["configuration"] = body.get("configuration", {})
        return self._inv(inv_id)

    def _remind(self, inv_id, body, **_):
        if self.invoices[inv_id]["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
            raise PayPalError(422, {"name": "CANNOT_REMIND"})
        self.reminders.append((inv_id, body))
        return {}

    def _cancel_invoice(self, inv_id, **_):
        if self.invoices[inv_id]["status"] in ("PAID", "MARKED_AS_PAID", "PARTIALLY_PAID"):
            raise PayPalError(422, {"name": "UNPROCESSABLE_ENTITY",
                                    "details": [{"issue": "CANNOT_CANCEL_PAID_INVOICE"}]})
        self.invoices[inv_id]["status"] = "CANCELLED"
        return {}

    def _record_payment(self, inv_id, body, **_):
        inv = self.invoices[inv_id]
        due = round(float(inv["due_amount"]["value"]) - float(body["amount"]["value"]), 2)
        inv["due_amount"]["value"] = f"{max(due, 0):.2f}"
        inv["status"] = "MARKED_AS_PAID" if due <= 0 else "PARTIALLY_PAID"
        return {}

    def _search(self, body, **_):
        return {"items": [self._inv(i) for i, v in self.invoices.items()
                          if v["status"] in body.get("status", [])]}

    # helpers for tests: act as the client
    def client_pays(self, inv_id, amount=None):
        inv = self.invoices[inv_id]
        self._record_payment(inv_id, {"amount": {"value": str(amount or inv["due_amount"]["value"])}})
        if amount is None:
            inv["status"] = "PAID"

    def client_opens(self, inv_id):
        self.invoices[inv_id]["detail"]["viewed_by_recipient"] = True

    # ---------- payouts ----------

    def _create_batch(self, body, **_):
        sender_id = body["sender_batch_header"]["sender_batch_id"]
        if any(b["sender_batch_id"] == sender_id for b in self.batches.values()):
            raise PayPalError(400, {"name": "USER_BUSINESS_ERROR",
                                    "message": "Batch with given sender_batch_id already exists"})
        batch_id = self._id("B")
        items = []
        for it in body["items"]:
            item_id = self._id("ITEM")
            status = "UNCLAIMED" if it["receiver"] in self.unclaimed_emails else "SUCCESS"
            self.items[item_id] = {"payout_item_id": item_id, "transaction_status": status,
                                   "payout_batch_id": batch_id, "payout_item": it}
            items.append(item_id)
        self.batches[batch_id] = {"sender_batch_id": sender_id, "items": items,
                                  "subject": body["sender_batch_header"].get("email_subject")}
        return {"batch_header": {"payout_batch_id": batch_id, "batch_status": "PENDING",
                                 "sender_batch_header": body["sender_batch_header"]}}

    def _item_view(self, item_id):
        return json.loads(json.dumps(self.items[item_id]))

    def _get_batch(self, batch_id, **_):
        b = self.batches[batch_id]
        items = [self._item_view(i) for i in b["items"]]
        done = all(i["transaction_status"] != "PENDING" for i in items)
        return {"batch_header": {"payout_batch_id": batch_id,
                                 "batch_status": "SUCCESS" if done else "PROCESSING"},
                "items": items}

    def _cancel_item(self, item_id, **_):
        item = self.items[item_id]
        if item["transaction_status"] != "UNCLAIMED":
            raise PayPalError(422, {"name": "ITEM_ALREADY_CLAIMED"})
        item["transaction_status"] = "RETURNED"
        return self._item_view(item_id)

    # ---------- subscriptions ----------

    def _create_sub(self, body, **_):
        sub_id = self._id("I")
        self.subs[sub_id] = {"id": sub_id, "status": "APPROVAL_PENDING", "plan_id": body["plan_id"],
                             "start_time": body.get("start_time"),
                             "links": [{"rel": "approve",
                                        "href": f"https://www.sandbox.paypal.com/approve/{sub_id}"}],
                             "billing_info": {}}
        return self.subs[sub_id]

    def _sub_action(self, sub_id, action, **_):
        self.subs[sub_id]["status"] = {"suspend": "SUSPENDED", "activate": "ACTIVE",
                                       "cancel": "CANCELLED"}[action]
        return {}

    def client_approves(self, sub_id, next_billing="2026-11-23T10:00:00Z", paid=None,
                        paid_at="2026-10-23T10:00:00Z"):
        s = self.subs[sub_id]
        s["status"] = "ACTIVE"
        s["billing_info"] = {"next_billing_time": next_billing}
        if paid is not None:
            s["billing_info"]["last_payment"] = {"time": paid_at,
                                                 "amount": {"value": f"{paid:.2f}"}}

    # ---------- orders (donations) ----------

    def _create_order(self, body, **_):
        order_id = self._id("O")
        self.orders[order_id] = {"id": order_id, "status": "PAYER_ACTION_REQUIRED",
                                 "purchase_units": body["purchase_units"],
                                 "links": [{"rel": "payer-action",
                                            "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}]}
        return self.orders[order_id]

    def _capture_order(self, order_id, **_):
        self.orders[order_id]["status"] = "COMPLETED"
        return self.orders[order_id]


class FakeLLM:
    """Scripted AI. Queue replies with .reply(text) / .tool(name, args); or set .responder to a
    function(body) -> message dict. Records every request body."""

    def __init__(self):
        self.requests: list[dict] = []
        self.queue: list[dict] = []
        self.responder = None

    def reply(self, text):
        self.queue.append({"role": "assistant", "content": text})
        return self

    def tool(self, name, args, content=""):
        self.queue.append({"role": "assistant", "content": content, "tool_calls": [{
            "id": f"call_{len(self.queue)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]})
        return self

    def __call__(self, body):
        self.requests.append(body)
        if self.responder:
            return self.responder(body)
        if not self.queue:
            raise AssertionError("FakeLLM was called but has no reply queued")
        return self.queue.pop(0)

    def prompt(self, i=-1) -> str:
        """All text sent in request i, for checking what the AI was told."""
        return "\n".join(str(m.get("content")) for m in self.requests[i]["messages"])


class FakeGitHub:
    """Dict-backed GitHub: api[path] -> JSON, files[(owner, repo, path)] -> text, trees[...]."""

    def __init__(self):
        self.api: dict[str, object] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        self.trees: dict[tuple[str, str], list[str]] = {}

    def api_get(self, path, params=None):
        from backend.oss.github import NotFound
        key = path
        if params and "page" in params:
            key = f"{path}?page={params['page']}"
            if key not in self.api and params["page"] == 1:
                key = path
        if key not in self.api:
            if params and params.get("page", 1) > 1:
                return []
            raise NotFound(404, f"Not found on GitHub: {path}")
        return self.api[key]

    def raw_file(self, owner, repo, ref, path):
        return self.files.get((owner, repo, path))

    def get_tree(self, owner, repo, branch):
        return self.trees.get((owner, repo), [])
