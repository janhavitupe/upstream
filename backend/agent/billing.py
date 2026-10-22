"""Chat billing agent (Module 2): plain-language requests -> PayPal invoices.

Money in is autonomous: invoices are created and sent at once, and every one can be undone
(cancelled). The model only calls tools; the tools validate everything and do the work.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from backend import config, db
from backend.paypal import invoices as pp_invoices
from backend.paypal.client import PayPalError

from .llm import LLMError, chat

MAX_STEPS = 6
OPEN = ("SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED")

SYSTEM = """You are Upstream's billing assistant for {invoicer}, an independent developer.
Today is {today}. Calendar for the coming weeks (use it for every date; don't count days
yourself): {calendar}
You create and manage PayPal invoices by calling tools.
Rules:
- To bill someone, call create_invoice. It is sent immediately; the user can undo it.
- Use the client's name as the user wrote it. Never invent an email address: if create_invoice
  says the client is unknown, ask the user for the client's email, then call add_client and retry.
- Give the due date as due_date (YYYY-MM-DD) read from the calendar. "this Friday" is the
  coming Friday; "next Friday" is the Friday of next week. If no due date is given, leave it
  out (it defaults to 14 days).
- Keep amounts exactly as stated. Currency defaults to USD.
- If a request is ambiguous (which invoice, which client, what amount), ask one short question.
- After acting, reply in one short sentence. Don't repeat details shown on the card, and don't
  add filler like "let me know if you need anything else"."""

TOOLS = [
    {"type": "function", "function": {
        "name": "create_invoice",
        "description": "Create a PayPal invoice and send it to the client right away.",
        "parameters": {"type": "object", "properties": {
            "client_name": {"type": "string", "description": "Client as the user named them"},
            "items": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "quantity": {"type": "number"},
                "unit_price": {"type": "number", "description": "Price per unit"}},
                "required": ["name", "unit_price"]}},
            "due_date": {"type": "string", "description": "YYYY-MM-DD from the calendar"},
            "currency": {"type": "string", "description": "ISO code, default USD"},
            "note": {"type": "string", "description": "Optional note to the client"}},
            "required": ["client_name", "items"]}}},
    {"type": "function", "function": {
        "name": "add_client",
        "description": "Save a client's name and email so they can be invoiced by name.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "email": {"type": "string"}},
            "required": ["name", "email"]}}},
    {"type": "function", "function": {
        "name": "list_invoices",
        "description": "List invoices. Use for questions like 'who owes me money?'.",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string", "enum": ["open", "overdue", "paid", "all"]},
            "client_name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "cancel_invoice",
        "description": "Cancel an unpaid invoice (by its number, or the latest open one for a client).",
        "parameters": {"type": "object", "properties": {
            "invoice_number": {"type": "string"}, "client_name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "set_chasing",
        "description": "Turn automatic overdue reminders on or off, for everyone or one client.",
        "parameters": {"type": "object", "properties": {
            "enabled": {"type": "boolean"},
            "client_name": {"type": "string", "description": "Omit to change it for everyone"}},
            "required": ["enabled"]}}},
]


class ToolError(Exception):
    pass


def _payer_url(inv: dict) -> str | None:
    return ((inv.get("detail") or {}).get("metadata") or {}).get("recipient_view_url")


def _card(row: dict) -> dict:
    keys = ("id", "number", "client_name", "client_email", "amount", "currency", "due_date",
            "status", "payer_url", "items")
    return {k: row.get(k) for k in keys}


def _find_open(invoice_number: str | None, client_name: str | None) -> dict:
    rows = db.list_invoices(OPEN)
    if invoice_number:
        n = invoice_number.lstrip("#").strip()
        rows = [r for r in rows if (r["number"] or "").lstrip("0") == n.lstrip("0") or r["id"] == n]
    if client_name:
        c = client_name.lower()
        rows = [r for r in rows if c in (r["client_name"] or "").lower()
                or c in r["client_email"].lower()]
    if not rows:
        raise ToolError("No matching unpaid invoice.")
    if len(rows) > 1 and not invoice_number:
        raise ToolError("More than one unpaid invoice matches: "
                        + ", ".join(f"#{r['number']} ({r['client_name']}, {r['amount']:.2f})"
                                    for r in rows[:5]) + ". Ask which one.")
    return rows[0]


def create_invoice(client_name: str, items: list[dict], due_date: str | None = None,
                   due_in_days: int | None = None, currency: str = "USD",
                   note: str | None = None) -> dict:
    client = db.find_client(client_name)
    if not client:
        raise UnknownClient(client_name)
    clean = []
    for it in items or []:
        qty, price = float(it.get("quantity") or 1), float(it.get("unit_price") or 0)
        if not it.get("name") or qty <= 0 or price <= 0:
            raise ToolError(f"Invalid line item: {it}")
        clean.append({"name": str(it["name"])[:200], "quantity": qty, "unit_price": price})
    if not clean:
        raise ToolError("An invoice needs at least one item.")
    total = round(sum(i["quantity"] * i["unit_price"] for i in clean), 2)
    if total > config.INVOICE_MAX:
        raise ToolError(f"Total {total:.2f} is above the {config.INVOICE_MAX:.2f} limit for "
                        "chat invoices. Ask the user to confirm the amount.")
    if due_date:
        try:
            due_in_days = (date.fromisoformat(due_date) - date.today()).days
        except ValueError:
            raise ToolError(f"Due date '{due_date}' isn't YYYY-MM-DD.")
        if due_in_days < 0:
            raise ToolError(f"Due date {due_date} is in the past.")
    due_in_days = max(0, min(int(14 if due_in_days is None else due_in_days), 365))
    currency = (currency or "USD").upper()[:3]
    inv = pp_invoices.create_and_send(client["email"], clean, due_in_days=due_in_days,
                                      note=note, currency=currency,
                                      recipient_name=client["name"])
    s = pp_invoices.simplify(inv)
    row = {"id": s["id"], "number": s["number"], "client_name": client["name"],
           "client_email": client["email"], "amount": float(s["amount"] or total),
           "currency": s["currency"] or currency, "due_date": s["due_date"]
           or (date.today() + timedelta(days=due_in_days)).isoformat(),
           "status": s["status"], "payer_url": _payer_url(inv), "items": clean}
    db.save_invoice(row)
    return _card(row)


def add_client(name: str, email: str) -> dict:
    if "@" not in (email or "") or not (name or "").strip():
        raise ToolError("A client needs a name and a valid email address.")
    return db.upsert_client(name, email)


def list_invoices(filter: str = "open", client_name: str | None = None) -> list[dict]:
    statuses = {"open": OPEN, "overdue": OPEN, "paid": ("PAID", "MARKED_AS_PAID"),
                "all": None}.get(filter or "open", OPEN)
    rows = db.list_invoices(statuses)
    if filter == "overdue":
        rows = [r for r in rows if r["due_date"] < date.today().isoformat()]
    if client_name:
        rows = [r for r in rows if client_name.lower() in (r["client_name"] or "").lower()]
    return [{"number": r["number"], "client": r["client_name"], "amount": r["amount"],
             "currency": r["currency"], "due_date": r["due_date"], "status": r["status"],
             "reminders_sent": r["chase_stage"]} for r in rows[:25]]


def cancel_invoice(invoice_number: str | None = None, client_name: str | None = None) -> dict:
    row = _find_open(invoice_number, client_name)
    pp_invoices.cancel(row["id"])
    db.update_invoice(row["id"], status="CANCELLED")
    return _card({**row, "status": "CANCELLED"})


def set_chasing(enabled: bool, client_name: str | None = None) -> dict:
    if client_name:
        client = db.find_client(client_name)
        if not client:
            raise ToolError(f"Unknown client '{client_name}'.")
        db.set_client_chase(client["name"], not enabled)
        return {"client": client["name"], "chasing": enabled}
    db.set_setting("chasing_paused", not enabled)
    return {"everyone": True, "chasing": enabled}


HANDLERS = {"create_invoice": create_invoice, "add_client": add_client,
            "list_invoices": list_invoices, "cancel_invoice": cancel_invoice,
            "set_chasing": set_chasing}
CARD_TOOLS = {"create_invoice": "invoice_sent", "cancel_invoice": "invoice_cancelled",
              "set_chasing": "chasing_changed"}


def _clean(text: str) -> str:
    """Normalise odd spacing the model sometimes emits and drop empty brackets."""
    text = re.sub(r"[   ]", " ", text)
    text = re.sub(r"[​-‍⁠﻿]", "", text)
    text = re.sub(r"\s*\(\s*\)", "", text)
    return re.sub(r"[ 	]{2,}", " ", text).strip()


def _dedupe(text: str) -> str:
    """The model sometimes emits its reply twice, glued together with no space
    ("...was cancelled.Invoice 0009 is cancelled."). Keep the first copy."""
    first = re.split(r"(?<=[.!?])(?=[A-Z‘“])", text, maxsplit=1)[0]
    return first if len(first) >= 12 else text


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class UnknownClient(ToolError):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Unknown client '{name}': no email on file. Ask the user for their "
                         "email address.")


def run(history: list[dict], user_message: str, pending: dict | None = None) -> dict:
    """history: earlier [{"role": "user"|"assistant", "content": str}] turns.

    pending: what the previous turn was waiting for, e.g. {"client_name": "Initech",
    "request": "Bill Initech $300 for a logo"} after an unknown client. If this message
    contains an email, the code saves it and has the model finish the original request,
    rather than trusting the model to remember to.
    """
    today = date.today()
    calendar = "; ".join(f"{d:%a %d %b}={d.isoformat()}"
                         for d in (today + timedelta(days=i) for i in range(1, 22)))
    messages = [{"role": "system", "content": SYSTEM.format(
        invoicer=config.INVOICER_NAME, today=today.strftime("%A %d %B %Y"), calendar=calendar)}]
    messages += [{"role": m["role"], "content": str(m["content"])[:2000]}
                 for m in history[-12:] if m.get("role") in ("user", "assistant")]
    messages.append({"role": "user", "content": user_message[:2000]})
    actions = []
    email = EMAIL_RE.search(user_message)
    if pending and pending.get("client_name") and email:
        client = db.upsert_client(pending["client_name"], email.group(0))
        actions.append({"type": "client_saved", "data": client})
        messages.append({"role": "system", "content": (
            f"Saved {client['name']}'s email ({client['email']}). Now complete the user's "
            f"earlier request with create_invoice: \"{str(pending.get('request', ''))[:500]}\"")})
    next_pending = None
    for _ in range(MAX_STEPS):
        try:
            msg = chat(messages, tools=TOOLS)
        except LLMError as e:
            # Say exactly what did happen: earlier steps may already have sent or cancelled
            done = (f" I did finish {len(actions)} step(s) first; see the card(s) below."
                    if actions else " Nothing was sent or changed.")
            return {"reply": f"The AI stopped partway ({e}).{done}", "actions": actions,
                    "pending": next_pending}
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"reply": _dedupe(_clean(msg.get("content") or "")) or "Done.",
                    "actions": actions, "pending": next_pending}
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = HANDLERS[name](**args)
                if name in CARD_TOOLS:
                    actions.append({"type": CARD_TOOLS[name], "data": result})
                if name == "create_invoice":
                    next_pending = None
                content = json.dumps({"ok": True, "result": result})
            except UnknownClient as e:
                next_pending = {"client_name": e.name, "request": user_message
                                if not pending else pending.get("request", user_message)}
                content = json.dumps({"ok": False, "error": str(e)})
            except (ToolError, PayPalError, KeyError, TypeError, ValueError) as e:
                detail = e.body if isinstance(e, PayPalError) else str(e)
                content = json.dumps({"ok": False, "error": str(detail)[:500]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
    return {"reply": "I couldn't finish that. Please try rephrasing.", "actions": actions,
            "pending": next_pending}
