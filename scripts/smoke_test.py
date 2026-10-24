"""
Upstream smoke test: checks every external API the app depends on, through the
same backend code the API routes use.

    python scripts/smoke_test.py                 # everything
    python scripts/smoke_test.py payouts orders  # only some sections

Sections: config, oauth, payouts, invoices, subscriptions, orders, github, llm
PayPal sections need PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET in .env (sandbox only).
Exit code is 0 when nothing failed (skipped checks don't count as failures).
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from backend.agent import llm  # noqa: E402
from backend import github  # noqa: E402
from backend.paypal import invoices, orders, payouts, subscriptions  # noqa: E402
from backend.paypal.client import PayPalError, paypal  # noqa: E402

RESULTS = {"PASS": 0, "FAIL": 0, "SKIP": 0}


def report(status: str, name: str, detail: str = ""):
    RESULTS[status] += 1
    print(f"[{status}] {name}" + (f": {detail}" if detail else ""))


def check(name: str, fn):
    """Run one check; returns its result, or None if it failed."""
    try:
        result = fn()
    except PayPalError as e:
        report("FAIL", name, f"HTTP {e.status} {str(e.body)[:400]}")
        return None
    except Exception as e:
        report("FAIL", name, f"{type(e).__name__}: {str(e)[:400]}")
        return None
    report("PASS", name)
    return result


def info(label: str, value):
    print(f"        {label} = {value}")


# ---------- sections ----------

def test_config():
    if "sandbox" not in config.PAYPAL_BASE:
        report("FAIL", "Config: PAYPAL_BASE is sandbox", config.PAYPAL_BASE)
    else:
        report("PASS", "Config: PAYPAL_BASE is sandbox")
    for label, value in [("DEMO_TEAM_EMAILS", config.DEMO_TEAM_EMAILS),
                         ("DEMO_CLIENT_EMAIL", config.DEMO_CLIENT_EMAIL),
                         ("DEMO_HOSPITAL_EMAIL", config.DEMO_HOSPITAL_EMAIL)]:
        if value:
            report("PASS", f"Config: {label} set")
        else:
            report("SKIP", f"Config: {label}", "not set; dependent checks will be skipped")
    if not config.PAYPAL_WEBHOOK_ID:
        report("SKIP", "Config: PAYPAL_WEBHOOK_ID", "webhook signatures won't be verified")


def test_oauth():
    token = check("OAuth: access token", paypal.token)
    return token is not None


def test_payouts():
    if not config.DEMO_TEAM_EMAILS:
        report("SKIP", "Payouts", "DEMO_TEAM_EMAILS not set")
        return
    items = [{"email": e, "amount": 1.00, "note": "Upstream smoke test",
              "item_id": f"smoke-{i}"} for i, e in enumerate(config.DEMO_TEAM_EMAILS)]
    resp = check("Payouts: create batch", lambda: payouts.create_batch(
        items, sender_batch_id=f"smoke-{uuid.uuid4().hex[:12]}"))
    if not resp:
        return
    batch_id = resp["batch_header"]["payout_batch_id"]
    info("payout_batch_id", batch_id)
    batch = check("Payouts: batch status", lambda: payouts.get_batch(batch_id))
    if batch:
        info("batch_status", batch["batch_header"]["batch_status"])
        first = (batch.get("items") or [{}])[0].get("payout_item_id")
        if first:
            item = check("Payouts: item status", lambda: payouts.get_item(first))
            if item:
                info("first item", item.get("transaction_status"))


def test_invoices():
    if not config.DEMO_CLIENT_EMAIL:
        report("SKIP", "Invoices", "DEMO_CLIENT_EMAIL not set")
        return
    inv = check("Invoices: create + send", lambda: invoices.create_and_send(
        config.DEMO_CLIENT_EMAIL,
        [{"name": "Smoke test: API integration", "quantity": 1, "unit_price": 1.00}],
        due_in_days=14, note="Upstream smoke test. Safe to ignore."))
    if not inv:
        return
    simple = invoices.simplify(inv)
    info("invoice", f"{simple['id']} ({simple['status']}, due {simple['due_date']})")
    check("Invoices: remind", lambda: invoices.remind(
        simple["id"], "Smoke test reminder. Safe to ignore."))
    found = check("Invoices: search open", invoices.list_open)
    if found is not None:
        info("open invoices", len(found))
    check("Invoices: cancel (cleanup)", lambda: invoices.cancel(simple["id"]))


def test_subscriptions():
    product = check("Subscriptions: create product",
                    lambda: subscriptions.create_product("Smoke test retainer"))
    if not product:
        return
    plan = check("Subscriptions: create plan", lambda: subscriptions.create_plan(
        product["id"], "Smoke test retainer: $1.00/month", 1.00))
    if not plan:
        return
    info("plan_id", plan["id"])
    sub = check("Subscriptions: create subscription", lambda: subscriptions.create_subscription(
        plan["id"], config.DEMO_CLIENT_EMAIL or None))
    if not sub:
        return
    info("subscription", f"{sub.get('id')} ({sub.get('status')})")
    if not subscriptions.approve_link(sub):
        report("FAIL", "Subscriptions: client approval link", "missing from response")
    else:
        report("PASS", "Subscriptions: client approval link")
    fetched = check("Subscriptions: get", lambda: subscriptions.get(sub["id"]))
    if fetched:
        info("status", fetched.get("status"))
    # Pause/resume/cancel need an ACTIVE subscription, i.e. a client who approved it in the
    # browser. PayPal returns 404 for cancel on APPROVAL_PENDING; those expire on their own.
    if (fetched or sub).get("status") == "APPROVAL_PENDING":
        report("SKIP", "Subscriptions: cancel (cleanup)", "not approved yet; it will expire")
    else:
        check("Subscriptions: cancel (cleanup)", lambda: subscriptions.cancel(sub["id"]))


def test_orders():
    if not config.DEMO_HOSPITAL_EMAIL:
        report("SKIP", "Orders", "DEMO_HOSPITAL_EMAIL not set")
        return
    order = check("Orders: create donation (payee = hospital)", lambda: orders.create_donation(
        1.00, config.DEMO_HOSPITAL_EMAIL, "Upstream smoke test donation"))
    if not order:
        return
    info("order", f"{order.get('id')} ({order.get('status')})")
    link = orders.approve_link(order)
    if link:
        report("PASS", "Orders: donor approval link")
        info("approve", link)
    else:
        report("FAIL", "Orders: donor approval link", "missing from response")


def test_github():
    repo = check("GitHub: repo info", lambda: github.repo_info("pallets", "flask"))
    if not repo:
        return
    if not config.GITHUB_TOKEN:
        report("SKIP", "GitHub: token", "not set; limit is 60 requests/hour")


def test_llm():
    if not config.llm_configured():
        report("SKIP", "LLM", "LLM_API_KEY not set; rule-based reasons will be used")
        return
    models = check(f"LLM: list models at {config.LLM_BASE_URL}", llm.list_models)
    if models is None:
        return
    if config.LLM_MODEL not in models:
        report("FAIL", f"LLM: model {config.LLM_MODEL} available",
               f"not offered; set LLM_MODEL to one of: {', '.join(models[:15])}")
        return
    reply = check(f"LLM: {config.LLM_MODEL} responds",
                  lambda: llm.complete("Reply with: ok", max_tokens=512))
    if reply is not None:
        info("reply", reply.strip()[:60])
    data = check("LLM: JSON mode", lambda: json.loads(llm.complete(
        'Return a JSON object {"ok": true}.', max_tokens=512, json_mode=True)))
    if data is not None:
        info("json", data)


PAYPAL_SECTIONS = {"payouts": test_payouts, "invoices": test_invoices,
                   "subscriptions": test_subscriptions, "orders": test_orders}
OTHER_SECTIONS = {"github": test_github, "llm": test_llm}


def main(argv: list[str]) -> int:
    known = {"config", "oauth", *PAYPAL_SECTIONS, *OTHER_SECTIONS}
    wanted = set(argv) or known
    unknown = wanted - known
    if unknown:
        print(f"Unknown section(s): {', '.join(sorted(unknown))}. Choose from: "
              f"{', '.join(sorted(known))}")
        return 2

    print(f"Upstream smoke test against {config.PAYPAL_BASE}\n")
    if "config" in wanted:
        test_config()

    needs_paypal = wanted & {"oauth", *PAYPAL_SECTIONS}
    if needs_paypal:
        if not config.paypal_configured():
            report("SKIP", "PayPal", "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set in .env")
        elif test_oauth():
            for name, fn in PAYPAL_SECTIONS.items():
                if name in wanted:
                    fn()
        else:
            report("SKIP", "PayPal APIs", "OAuth failed; check the client ID and secret")

    for name, fn in OTHER_SECTIONS.items():
        if name in wanted:
            fn()

    print(f"\n{RESULTS['PASS']} passed, {RESULTS['FAIL']} failed, {RESULTS['SKIP']} skipped")
    return 1 if RESULTS["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
