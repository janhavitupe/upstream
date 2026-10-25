"""API routes for Module 2 (Get paid): the billing chat, tracked invoices, clients."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import chasing, config, db, retainers
from backend.agent import billing
from backend.agent.llm import LLMError, complete
from backend.paypal import invoices as pp_invoices

router = APIRouter(prefix="/api/getpaid", tags=["2 · Get paid"])


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000,
                         examples=["Bill Acme $500 for the API integration, due in 14 days"])
    history: list[ChatTurn] = Field(default_factory=list)
    pending: dict | None = Field(None, description="Echo back the 'pending' from the last reply.")


@router.post("/chat")
def chat(body: ChatIn):
    return billing.run([t.model_dump() for t in body.history], body.message, body.pending)


def _invoice_view(inv: dict) -> dict:
    """Adds days overdue, what chasing will do next (by the demo clock) and "needs you"."""
    view = {**inv, "days_overdue": None, "next_reminder": None, "chasing": None,
            "owed": chasing.owed(inv), "needs_you": bool(inv["needs_you_at"]) and
            inv["status"] in chasing.OPEN}
    if inv["status"] in chasing.OPEN:
        view["days_overdue"] = max(0, chasing.days_overdue(inv))
        client = db.find_client(inv["client_name"]) if inv["client_name"] else None
        if db.get_setting("chasing_paused", False):
            view["chasing"] = "paused"
        elif client and client["dont_chase"]:
            view["chasing"] = "off for this client"
        else:
            view["chasing"] = "on"
            tone, on = chasing.next_step(inv)
            view["next_reminder"] = {"tone": tone, "on": on.isoformat()}
    return view


@router.get("/invoices")
def tracked_invoices():
    return [_invoice_view(i) for i in db.list_invoices()]


def _open_invoice(invoice_id: str) -> dict:
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    if inv["status"] not in billing.OPEN:
        raise HTTPException(409, f"This invoice is already {inv['status'].lower()}.")
    return inv


@router.get("/invoices/{invoice_id}/trail")
def paper_trail(invoice_id: str):
    """Everything that happened with an invoice: for a formal demand, collections or court."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    return {"invoice": _invoice_view(inv), "invoicer": config.INVOICER_NAME,
            "events": db.invoice_reminders(invoice_id)}


@router.post("/invoices/{invoice_id}/draft")
def draft_personal(invoice_id: str):
    """A personal message for the owner to send themselves (email, phone, chat). Not sent."""
    inv = _open_invoice(invoice_id)
    overdue = max(0, chasing.days_overdue(inv))
    seen = {1: "They have opened the invoice, so they know about it.",
            0: "They have never opened the invoice: the email may be wrong or unread."}.get(
        inv["viewed"], "")
    voice = db.get_setting("voice_samples", [])
    style = ("Write in the sender's own style, like these messages they wrote:\n"
             + "\n---\n".join(voice[:chasing.MAX_VOICE_SAMPLES])) if voice else \
        "Write warmly and plainly, like a person, not a company."
    prompt = (
        f"Draft a short personal message from {config.INVOICER_NAME} to "
        f"{inv['client_name'] or 'the client'}, to send directly (not through PayPal). Invoice "
        f"#{inv['number']} for ${chasing.owed(inv):,.2f} is {overdue} days overdue and "
        f"{inv['chase_stage']} automated reminders went unanswered. {seen}\n"
        "Goal: get a reply. Be human and direct, check in on whether something is wrong, ask "
        "when they can pay, and offer to split it into parts if that helps. No threats, no "
        "invented fees or deadlines. Under 90 words, plain text, no subject line. Greet "
        f"{inv['client_name'] or 'them'} by name and sign as {config.INVOICER_NAME} (or the "
        "name the sender signs with below). Never use placeholders in brackets: the message "
        "must be ready to send as is.\n" + style)
    try:
        text = complete(prompt, max_tokens=900).strip()
    except LLMError as e:
        raise HTTPException(503, f"The AI couldn't draft it right now: {e}")
    return {"draft": text, "to": inv["client_email"], "viewed": inv["viewed"]}


class PartialIn(BaseModel):
    minimum: float = Field(..., gt=0, description="Smallest payment the client may make.")


@router.post("/invoices/{invoice_id}/allow-partial")
def allow_partial(invoice_id: str, body: PartialIn):
    """Let the client pay in parts. PayPal updates the invoice and re-sends it."""
    inv = _open_invoice(invoice_id)
    if body.minimum >= chasing.owed(inv):
        raise HTTPException(400, "The minimum must be less than what's owed.")
    remote = pp_invoices.get(invoice_id)
    update = {k: remote[k] for k in ("detail", "invoicer", "primary_recipients", "items",
                                     "configuration") if k in remote}
    update["detail"] = {k: v for k, v in update["detail"].items() if k != "metadata"}
    update["configuration"] = {**update.get("configuration", {}), "partial_payment": {
        "allow_partial_payment": True,
        "minimum_amount_due": {"currency_code": inv["currency"], "value": f"{body.minimum:.2f}"}}}
    pp_invoices.paypal.request("PUT", f"/v2/invoicing/invoices/{invoice_id}", json=update,
                               params={"send_to_recipient": "true"})
    db.update_invoice(invoice_id, partial_allowed=1)
    db.save_event({"event_type": "UPSTREAM.INVOICE.PARTIAL_ALLOWED",
                   "resource": {"id": invoice_id}},
                  message=f"Let {inv['client_name'] or inv['client_email']} pay #{inv['number']} "
                          f"in parts (minimum ${body.minimum:,.2f}); PayPal re-sent it.",
                  verified=None)
    return _invoice_view(db.get_invoice(invoice_id))


@router.post("/invoices/{invoice_id}/write-off")
def write_off(invoice_id: str):
    """Give up on an invoice: cancel it in PayPal and stop chasing. Kept in the paper trail.

    PayPal won't cancel a partly paid invoice, so that one is closed on Upstream's side only
    (no more chasing); the client can still pay the rest through PayPal."""
    inv = _open_invoice(invoice_id)
    if inv["status"] == "PARTIALLY_PAID":
        db.update_invoice(invoice_id, status="WRITTEN_OFF", written_off=1)
    else:
        pp_invoices.cancel(invoice_id, note="This invoice has been closed.")
        db.update_invoice(invoice_id, status="CANCELLED", written_off=1)
    db.save_event({"event_type": "UPSTREAM.INVOICE.WRITTEN_OFF", "resource": {"id": invoice_id}},
                  message=f"Wrote off #{inv['number']} (${chasing.owed(inv):,.2f}) for "
                          f"{inv['client_name'] or inv['client_email']}.", verified=None)
    return _invoice_view(db.get_invoice(invoice_id))


@router.get("/state")
def state():
    """Switches, the demo clock and the chasing / payment log for the Get paid tab."""
    log = db.list_events(20, prefixes=("UPSTREAM.CHASE.", "UPSTREAM.INVOICE.", "UPSTREAM.RETAINER."))
    return {"chasing_paused": bool(db.get_setting("chasing_paused", False)),
            "sandbox": "sandbox" in config.PAYPAL_BASE,
            "demo_offset_days": chasing.demo_offset_days(),
            "today": chasing.today().isoformat(),
            "clients": db.list_clients(), "log": log[:15]}


class ToggleIn(BaseModel):
    enabled: bool


@router.post("/chasing")
def set_chasing(body: ToggleIn):
    db.set_setting("chasing_paused", not body.enabled)
    return state()


@router.post("/clients/{name}/chasing")
def set_client_chasing(name: str, body: ToggleIn):
    if not db.set_client_chase(name, not body.enabled):
        raise HTTPException(404, "Client not found.")
    return state()


class ClockIn(BaseModel):
    days: int = Field(..., ge=0, le=60)


@router.post("/demo-clock")
def demo_clock(body: ClockIn):
    """Sandbox only: pretend today is N days later, then run chasing and retainers at once."""
    if "sandbox" not in config.PAYPAL_BASE:
        raise HTTPException(400, "The demo clock only works in the PayPal sandbox.")
    db.set_setting("demo_offset_days", body.days)
    return {"actions": chasing.tick(force_refresh=True) + retainers.tick(force_refresh=True),
            **state()}


@router.post("/check")
def check_now():
    """Check PayPal for payments and send any reminders that are due, right now."""
    return {"actions": chasing.tick(force_refresh=True) + retainers.tick(force_refresh=True),
            **state()}


@router.post("/invoices/{invoice_id}/cancel")
def cancel(invoice_id: str):
    """Undo: cancel an invoice Upstream sent."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    if inv["status"] not in billing.OPEN:
        raise HTTPException(409, f"This invoice is already {inv['status'].lower()}.")
    pp_invoices.cancel(invoice_id)
    db.update_invoice(invoice_id, status="CANCELLED")
    return db.get_invoice(invoice_id)


@router.get("/clients")
def clients():
    return {"clients": db.list_clients(),
            "chasing_paused": bool(db.get_setting("chasing_paused", False))}


# ---------- your writing style for reminders ----------

class VoiceIn(BaseModel):
    samples: list[str] = Field(..., max_length=chasing.MAX_VOICE_SAMPLES,
                               description="Messages you've written to clients (up to 5).")


def _clean_samples(samples: list[str]) -> list[str]:
    out = [s.strip()[:1500] for s in samples if s and s.strip()]
    if any(len(s) < 15 for s in out):
        raise HTTPException(400, "Each sample should be a real message, at least a sentence long.")
    return out[:chasing.MAX_VOICE_SAMPLES]


@router.get("/voice")
def get_voice():
    return {"samples": db.get_setting("voice_samples", [])}


@router.put("/voice")
def set_voice(body: VoiceIn):
    """Save samples; every reminder from now on is written in this style. [] clears it."""
    db.set_setting("voice_samples", _clean_samples(body.samples))
    return get_voice()


class PreviewIn(BaseModel):
    tone: str = Field("friendly", pattern="^(friendly|firm|urgent|final)$")
    samples: list[str] | None = Field(None, description="Try samples before saving; "
                                                        "omit to use the saved ones.")


@router.post("/voice/preview")
def preview_voice(body: PreviewIn):
    """The same reminder written generically and in your voice, for an open invoice
    (or a sample one). Nothing is sent."""
    samples = (_clean_samples(body.samples) if body.samples is not None
               else db.get_setting("voice_samples", []))
    days = {"friendly": 2, "firm": 8, "urgent": 2, "final": 15}[body.tone]
    inv = next(iter(db.list_invoices(chasing.OPEN)), None) or {
        "number": "0042", "client_name": "Acme", "amount": 500.0, "currency": "USD",
        "due_date": (chasing.today() - chasing.timedelta(days=days)).isoformat(),
        "items": [{"name": "API integration"}]}
    return {"invoice": {k: inv[k] for k in ("number", "client_name", "amount", "due_date")},
            "tone": body.tone,
            "generic": chasing.write_reminder(inv, body.tone, days, voice=[]),
            "yours": chasing.write_reminder(inv, body.tone, days, voice=samples)
            if samples else None}


# ---------- retainers ----------

def _retainer_view(r: dict) -> dict:
    return {**retainers.view(r), "work": db.work_since(r["id"], None)[-10:],
            "emailed": bool(config.SMTP_HOST)}


@router.get("/retainers")
def list_retainers():
    return [_retainer_view(r) for r in db.list_retainers()]


def _retainer(retainer_id: str) -> dict:
    r = db.get_retainer(retainer_id)
    if not r:
        raise HTTPException(404, "Retainer not found.")
    return r


# Registered before the generic /{action} route, which would otherwise match "work"
class WorkIn(BaseModel):
    note: str = Field(..., min_length=3, max_length=500)


@router.post("/retainers/{retainer_id}/work")
def log_work(retainer_id: str, body: WorkIn):
    """What you delivered this month; it goes into the next heads-up."""
    _retainer(retainer_id)
    db.add_work(retainer_id, body.note)
    return _retainer_view(db.get_retainer(retainer_id))


@router.post("/retainers/{retainer_id}/{action}")
def change_retainer(retainer_id: str, action: str):
    _retainer(retainer_id)
    try:
        r = {"pause": lambda: retainers.pause(retainer_id, by="you"),
             "resume": lambda: retainers.resume(retainer_id, by="you"),
             "cancel": lambda: retainers.cancel(retainer_id),
             "refresh": lambda: (retainers.refresh(retainer_id), db.get_retainer(retainer_id))[1],
             }[action]()
    except KeyError:
        raise HTTPException(404, "Unknown action.")
    except retainers.RetainerError as e:
        raise HTTPException(409, str(e))
    return _retainer_view(db.get_retainer(retainer_id))
