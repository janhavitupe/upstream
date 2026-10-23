"""Fair retainers (Module 3): monthly PayPal subscriptions with honest renewals.

Before every renewal the client gets a heads-up: what's renewing, the amount and date, and
what they got last month (from the owner's work log), with a page where they can pause the
next renewal in one click. Payments and approvals are picked up from PayPal on their own.
"""
from __future__ import annotations

import secrets
import threading
from datetime import date, datetime, timedelta, timezone

from backend import chasing, config, db
from backend.agent.llm import LLMError, complete
from backend.paypal import subscriptions as subs
from backend.paypal.client import PayPalError

HEADS_UP_DAYS = 3  # how long before each renewal the client hears about it
LIVE = ("APPROVAL_PENDING", "APPROVED", "ACTIVE", "SUSPENDED")
_lock = threading.Lock()
_last_refresh = datetime.min.replace(tzinfo=timezone.utc)


class RetainerError(Exception):
    pass


def client_url(r: dict) -> str:
    return f"{config.APP_BASE_URL}/r/{r['token']}"


def _date(iso_time: str | None) -> str | None:
    return iso_time[:10] if iso_time else None


def create(client: dict, service: str, amount: float, start_date: str | None = None,
           currency: str = "USD") -> dict:
    """client: a row from db.find_client. start_date: YYYY-MM-DD of the first charge."""
    if amount <= 0 or amount > config.INVOICE_MAX:
        raise RetainerError(f"Monthly amount must be between 0 and {config.INVOICE_MAX:,.0f}.")
    start_time = None
    if start_date:
        try:
            start = date.fromisoformat(start_date)
        except ValueError:
            raise RetainerError(f"Start date '{start_date}' isn't YYYY-MM-DD.")
        if start < date.today():
            raise RetainerError("The start date is in the past.")
        if start > date.today():
            start_time = f"{start.isoformat()}T09:00:00Z"
    product = subs.create_product(f"{service} for {client['name']}")
    plan = subs.create_plan(product["id"], f"{service}: {currency} {amount:,.2f}/month", amount,
                            currency)
    sub = subs.create_subscription(plan["id"], client["email"], start_time)
    row = {"id": sub["id"], "client_name": client["name"], "client_email": client["email"],
           "service": service, "amount": round(amount, 2), "currency": currency,
           "plan_id": plan["id"], "status": sub.get("status", "APPROVAL_PENDING"),
           "approve_url": subs.approve_link(sub), "token": secrets.token_urlsafe(12),
           "next_billing": start_date}
    db.save_retainer(row)
    return view(db.get_retainer(row["id"]))


def view(r: dict) -> dict:
    out = {**r, "client_url": client_url(r), "next_heads_up": None}
    if r["status"] == "ACTIVE" and r["next_billing"]:
        out["next_heads_up"] = (date.fromisoformat(r["next_billing"])
                                - timedelta(days=HEADS_UP_DAYS)).isoformat()
    return out


# ---------- PayPal: approval, payments, status ----------

def refresh(retainer_id: str | None = None, force: bool = False) -> list[str]:
    global _last_refresh
    now = datetime.now(timezone.utc)
    if not retainer_id and not force and now - _last_refresh < chasing.REFRESH_EVERY:
        return []
    if not retainer_id:
        _last_refresh = now
    changes = []
    rows = [db.get_retainer(retainer_id)] if retainer_id else db.list_retainers(False)
    for r in filter(None, rows):
        if r["status"] not in LIVE:
            continue
        try:
            sub = subs.get(r["id"])
        except PayPalError as e:
            print(f"[retainers] refresh {r['id']}: {e}")
            continue
        status = sub.get("status") or r["status"]
        billing = sub.get("billing_info") or {}
        last = (billing.get("last_payment") or {}).get("time")
        fields = {"status": status, "next_billing": _date(billing.get("next_billing_time"))
                  or r["next_billing"]}
        who = r["client_name"] or r["client_email"]
        if status == "ACTIVE" and r["status"] in ("APPROVAL_PENDING", "APPROVED"):
            _event("ACTIVATED", r, f"{who} approved the {r['service']} retainer "
                                   f"({r['currency']} {r['amount']:,.2f}/month).")
            changes.append(f"activated {who}")
        if last and last != r["last_payment"]:
            fields["last_payment"] = last
            paid = float(((billing.get("last_payment") or {}).get("amount") or {}).get("value")
                         or r["amount"])
            _payment_landed(r, paid, last)
            changes.append(f"payment {who}")
        db.update_retainer(r["id"], **fields)
    return changes


def _payment_landed(r: dict, paid: float, when: str):
    who = r["client_name"] or r["client_email"]
    tax = round(paid * chasing.TAX_RATE, 2)
    auto = chasing.auto_give_back_percent()
    tail = ""
    if auto:
        give = round(paid * auto / 100, 2)
        if db.add_pot(give, f"{auto:g}% of {who}'s retainer payment", f"sub:{r['id']}:{when}"):
            tail = f" ${give:,.2f} ({auto:g}%) went into your upstream pot automatically."
    _event("PAYMENT", r, f"Retainer payment landed: {who} paid {r['currency']} {paid:,.2f} for "
                         f"{r['service']}. Set aside about ${tax:,.2f} for tax.{tail}")


def _event(kind: str, r: dict, message: str, **extra):
    db.save_event({"event_type": f"UPSTREAM.RETAINER.{kind}", "resource": {"id": r["id"]},
                   **extra}, message=message, verified=None)


# ---------- the heads-up before each renewal ----------

def write_heads_up(r: dict, work: list[dict]) -> str:
    who = r["client_name"] or "there"
    renews = date.fromisoformat(r["next_billing"])
    when = f"{renews:%b} {renews.day}"
    done = "; ".join(w["note"] for w in work)
    fallback = (f"Hi {who}, a heads-up that your {r['service']} retainer renews on {when} for "
                f"{r['currency']} {r['amount']:,.2f}."
                + (f" This month: {done}." if done else "")
                + f" If you'd like to pause the next renewal, you can do it here: {client_url(r)}")
    if not config.llm_configured():
        return fallback
    voice = db.get_setting("voice_samples", [])
    style = ("Write in the sender's own style, like these messages they wrote:\n"
             + "\n---\n".join(voice[:chasing.MAX_VOICE_SAMPLES])) if voice else \
        "Write warmly and plainly."
    prompt = (
        f"Write a short pre-renewal heads-up from {config.INVOICER_NAME} to {who}.\n"
        f"Their '{r['service']}' retainer renews on {when} for {r['currency']} "
        f"{r['amount']:,.2f}, charged automatically by PayPal.\n"
        + (f"Work delivered this month (mention it, briefly and concretely): {done}\n" if done
           else "No work log this month: don't invent any deliverables.\n")
        + f"Say clearly they can pause the next renewal at this link: {client_url(r)}\n"
        f"Greet them by name. Under 90 words, plain text, no subject line, no placeholders. "
        f"Don't invent facts.\n{style}")
    try:
        text = complete(prompt, max_tokens=900).strip()
        if client_url(r) not in text:
            text += f"\n\nPause the next renewal: {client_url(r)}"
        return text if 30 <= len(text) <= 1500 else fallback
    except LLMError as e:
        print(f"[retainers] AI heads-up skipped: {e}")
        return fallback


def heads_up_due() -> list[str]:
    sent = []
    for r in db.list_retainers(False):
        if r["status"] != "ACTIVE" or not r["next_billing"] or r["heads_up_for"] == r["next_billing"]:
            continue
        renews = date.fromisoformat(r["next_billing"])
        if chasing.today() < renews - timedelta(days=HEADS_UP_DAYS):
            continue
        work = db.work_since(r["id"], _last_heads_up_time(r))  # everything since the last one
        note = write_heads_up(r, work)
        db.update_retainer(r["id"], heads_up_for=r["next_billing"], heads_up_note=note)
        delivered = _deliver(r, note)
        _event("HEADS_UP", r, f"Heads-up for {r['client_name'] or r['client_email']}: the "
                              f"{r['service']} retainer renews on {r['next_billing']} for "
                              f"{r['currency']} {r['amount']:,.2f}. "
                              + ("Emailed to the client." if delivered else
                                 "Ready to send: copy it from the retainer card."),
               note=note, client_url=client_url(r))
        sent.append(f"heads-up {r['client_name']}")
    return sent


def _last_heads_up_time(r: dict) -> str | None:
    """Work logged since the previous heads-up belongs to this one."""
    with db.connect() as c:
        row = c.execute("SELECT received_at FROM events WHERE resource_id = ? AND event_type = "
                        "'UPSTREAM.RETAINER.HEADS_UP' ORDER BY id DESC LIMIT 1",
                        (r["id"],)).fetchone()
    return row["received_at"] if row else None


def _last_heads_up_time_before_latest(r: dict) -> str | None:
    """Start of "this period" on the client page: the heads-up before the latest one, so the
    page shows what the latest heads-up talked about plus anything logged since."""
    with db.connect() as c:
        rows = c.execute("SELECT received_at FROM events WHERE resource_id = ? AND event_type = "
                         "'UPSTREAM.RETAINER.HEADS_UP' ORDER BY id DESC LIMIT 2",
                         (r["id"],)).fetchall()
    return rows[1]["received_at"] if len(rows) == 2 else None


def _deliver(r: dict, note: str) -> bool:
    """Email the heads-up if SMTP is configured; otherwise the owner sends it."""
    if not config.SMTP_HOST:
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = r["client_email"]
    msg["Subject"] = f"Your {r['service']} retainer renews on {r['next_billing']}"
    msg.set_content(note)
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
            s.starttls()
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        print(f"[retainers] email failed: {e}")
        return False


# ---------- pause / resume / cancel ----------

def pause(retainer_id: str, by: str) -> dict:
    r = db.get_retainer(retainer_id)
    if not r or r["status"] != "ACTIVE":
        raise RetainerError("Only an active retainer can be paused.")
    subs.suspend(r["id"], reason=f"Paused by the {'client' if by == 'client' else 'owner'}")
    db.update_retainer(r["id"], status="SUSPENDED", paused_by=by)
    who = r["client_name"] or r["client_email"]
    _event("PAUSED", r, f"{'The client' if by == 'client' else 'You'} paused {who}'s "
                        f"{r['service']} retainer; no renewal until it's resumed.")
    return view(db.get_retainer(r["id"]))


def resume(retainer_id: str, by: str) -> dict:
    r = db.get_retainer(retainer_id)
    if not r or r["status"] != "SUSPENDED":
        raise RetainerError("Only a paused retainer can be resumed.")
    subs.activate(r["id"])
    db.update_retainer(r["id"], status="ACTIVE", paused_by=None)
    _event("RESUMED", r, f"{'The client' if by == 'client' else 'You'} resumed "
                         f"{r['client_name'] or r['client_email']}'s {r['service']} retainer.")
    refresh(r["id"])
    return view(db.get_retainer(r["id"]))


def cancel(retainer_id: str) -> dict:
    r = db.get_retainer(retainer_id)
    if not r or r["status"] == "CANCELLED":
        raise RetainerError("This retainer is already cancelled.")
    if r["status"] in ("ACTIVE", "SUSPENDED"):
        subs.cancel(r["id"])
    db.update_retainer(r["id"], status="CANCELLED")
    _event("CANCELLED", r, f"Cancelled {r['client_name'] or r['client_email']}'s "
                           f"{r['service']} retainer.")
    return view(db.get_retainer(r["id"]))


def tick(force_refresh: bool = False) -> list[str]:
    with _lock:
        return refresh(force=force_refresh) + heads_up_due()
