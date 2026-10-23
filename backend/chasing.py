"""Automatic invoice chasing (Module 2). Money in runs on its own, until it is paid.

Each tick: refresh open invoices from PayPal (paid ones stop being chased and produce a
"payment landed" note), then send the next reminder for each overdue invoice, getting firmer
each time. Respects the global pause and per-client "don't chase". In the sandbox a demo
clock can move "today" forward, so a week of chasing fits in a demo video.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone

from backend import config, db
from backend.agent.llm import LLMError, complete
from backend.paypal import invoices as pp_invoices
from backend.paypal.client import PayPalError

OPEN = ("SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED")
PAID = ("PAID", "MARKED_AS_PAID")
# (days overdue before this reminder, tone, email subject)
STAGES = [(1, "friendly", "Friendly reminder: invoice #{number}"),
          (7, "firm", "Second reminder: invoice #{number} is overdue"),
          (14, "final", "Final notice: invoice #{number}")]
REFRESH_EVERY = timedelta(minutes=2)  # how often open invoices are re-checked with PayPal
MAX_VOICE_SAMPLES = 5
DEFAULT_FOLLOWUP_DAYS = 7   # after the final notice, keep following up this often
NEEDS_YOU_AFTER_DAYS = 7    # ...and hand it to the owner this long after the final
TAX_RATE = 0.25       # suggested set-aside when a payment lands
GIVE_BACK_RATE = 0.02  # suggested share for the open source you depend on
_lock = threading.Lock()
_last_refresh = datetime.min.replace(tzinfo=timezone.utc)


# ---------- the demo clock ----------

def demo_offset_days() -> int:
    if "sandbox" not in config.PAYPAL_BASE:
        return 0  # never in live mode
    return int(db.get_setting("demo_offset_days", 0))


def today() -> date:
    return date.today() + timedelta(days=demo_offset_days())


def days_overdue(inv: dict) -> int:
    return (today() - date.fromisoformat(inv["due_date"])).days


# ---------- paid detection ----------

def mark_paid(invoice_id: str, status: str = "PAID") -> bool:
    """Record a payment once. Returns True the first time, so the note is posted once."""
    inv = db.get_invoice(invoice_id)
    if not inv or inv["status"] in PAID:
        return False
    db.update_invoice(invoice_id, status=status, paid_at=db.now())
    who = inv["client_name"] or inv["client_email"]
    tax = round(inv["amount"] * TAX_RATE, 2)
    auto = auto_give_back_percent()
    give = round(inv["amount"] * (auto or GIVE_BACK_RATE * 100) / 100, 2)
    if auto and give > 0:  # set aside on its own; it's only spent when an OSS payout is approved
        db.add_pot(give, f"{auto:g}% of {who}'s payment of #{inv['number']}", invoice_id)
        tail = f"${give:,.2f} ({auto:g}%) went into your upstream pot automatically."
    else:
        tail = f"Consider giving ${give:,.2f} to the open source you depend on."
    db.save_event({"event_type": "UPSTREAM.INVOICE.PAID", "resource": {"id": invoice_id},
                   "amount": inv["amount"], "tax": tax, "give_back": give},
                  message=f"Payment landed: {who} paid ${inv['amount']:,.2f} (#{inv['number']}). "
                          f"Set aside about ${tax:,.2f} for tax. {tail}", verified=None)
    return True


def auto_give_back_percent() -> float:
    return float(db.get_setting("auto_give_back_percent", 0))


def refresh_open(force: bool = False) -> list[str]:
    """Re-check open invoices with PayPal: payments, partial payments, and whether the
    client has opened the invoice at all."""
    global _last_refresh
    now = datetime.now(timezone.utc)
    if not force and now - _last_refresh < REFRESH_EVERY:
        return []
    _last_refresh = now
    changes = []
    for inv in db.list_invoices(OPEN):
        try:
            remote = pp_invoices.get(inv["id"])
        except PayPalError as e:
            print(f"[chasing] refresh {inv['number']}: {e}")
            continue
        status = remote.get("status")
        if status in PAID:
            if mark_paid(inv["id"], status):
                changes.append(f"paid #{inv['number']}")
            continue
        fields = {"viewed": int(bool((remote.get("detail") or {}).get("viewed_by_recipient")))}
        due = (remote.get("due_amount") or {}).get("value")
        if due is not None:
            fields["amount_due"] = float(due)
            paid_now = round(owed(inv) - float(due), 2)
            if paid_now > 0:  # a partial payment landed since the last check
                who = inv["client_name"] or inv["client_email"]
                db.save_event({"event_type": "UPSTREAM.INVOICE.PARTIAL_PAYMENT",
                               "resource": {"id": inv["id"]}, "amount": paid_now},
                              message=f"Payment landed: {who} paid ${paid_now:,.2f} of "
                                      f"#{inv['number']}; ${float(due):,.2f} is still owed and "
                                      "will keep being chased.", verified=None)
                changes.append(f"partial payment #{inv['number']}")
        if status and status != inv["status"]:
            fields["status"] = status
            changes.append(f"#{inv['number']} {status.lower()}")
        db.update_invoice(inv["id"], **fields)
    return changes


def owed(inv: dict) -> float:
    return inv["amount_due"] if inv.get("amount_due") is not None else inv["amount"]


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"  # "Oct 8" (strftime's %-d isn't portable to Windows)


TONE_GUIDE = {
    "friendly": "gentle, assume good faith",
    "firm": "clear and direct, still polite",
    "final": "serious final notice: say plainly this is the final reminder and ask for payment "
             "now, no threats",
    "follow-up": "persistent and factual after the final notice: state how many reminders have "
                 "gone unanswered, ask for payment now or a reply about when it will be paid, "
                 "no threats",
}


def write_reminder(inv: dict, tone: str, overdue: int, voice: list[str] | None = None,
                   sent_before: int = 0) -> str:
    """A short note for the PayPal reminder. Falls back to a template without the AI.

    voice: sample messages to imitate; None uses the saved samples, [] means no style.
    sent_before: reminders already sent (used by follow-ups after the final notice)."""
    who = inv["client_name"] or "there"
    amount = owed(inv)
    fallback = {
        "friendly": f"Hi {who}, a quick reminder that invoice #{inv['number']} for "
                    f"${amount:,.2f} was due on {_day(inv['due_date'])}. If it's already on "
                    "its way, thank you! Otherwise you can pay it from this email.",
        "firm": f"Hi {who}, invoice #{inv['number']} for ${amount:,.2f} is now {overdue} days "
                "overdue. Please arrange payment this week, or let me know if something is "
                "holding it up.",
        "final": f"Hi {who}, this is a final reminder that invoice #{inv['number']} for "
                 f"${amount:,.2f} is {overdue} days overdue. Please pay it as soon as possible.",
        "follow-up": f"Hi {who}, invoice #{inv['number']} for ${amount:,.2f} is still unpaid, "
                     f"{overdue} days after it was due, and {sent_before} reminders have gone "
                     "unanswered. Please pay it from this email today, or reply to let me know "
                     "when it will be paid.",
    }[tone]
    if not config.llm_configured():
        return fallback
    if voice is None:
        voice = db.get_setting("voice_samples", [])
    style = ("Write AS the sender, imitating these real messages they wrote: the same kind of "
             "greeting, sign-off (and the name they sign with), formality, sentence length, "
             "punctuation and emoji use. Copy the style, not the content:\n"
             + "\n---\n".join(voice[:MAX_VOICE_SAMPLES])) if voice \
        else "Write in a warm, professional voice."
    partial = (f" (partly paid: ${amount:,.2f} of ${inv['amount']:,.2f} is still owed)"
               if amount < inv["amount"] else "")
    final_on = f" on {_day(inv['final_day'])}" if inv.get("final_day") else ""
    history = (f" {sent_before} earlier reminders, including a final notice{final_on}, got no "
               "payment. Only mention dates given here." if tone == "follow-up" else "")
    prompt = (
        f"Write the note for a PayPal invoice reminder from {config.INVOICER_NAME} to {who}.\n"
        f"Invoice #{inv['number']}, ${amount:,.2f} {inv['currency']} owed{partial}, due "
        f"{_day(inv['due_date'])}, now {overdue} day{'s' if overdue != 1 else ''} overdue. "
        f"Items: {', '.join(i['name'] for i in inv['items'])}.{history}\n"
        f"Tone: {tone} ({TONE_GUIDE[tone]}).\n"
        f"{style}\n"
        "The tone level always wins over the style: firmer stages are clearly firmer, even in a "
        "casual voice.\n"
        f"Greet them by name ({who}). Write dates like 'Oct 21'. Under 70 words. Plain text, no "
        "subject line, no placeholders like [Name]. Don't invent facts, late fees, deadlines or "
        "consequences. The client can pay from the email this note is in.")
    try:
        text = complete(prompt, max_tokens=800).strip()
        return text if 20 <= len(text) <= 1200 else fallback
    except LLMError as e:
        print(f"[chasing] AI note skipped: {e}")
        return fallback


def followup_days() -> int:
    return int(db.get_setting("followup_days", DEFAULT_FOLLOWUP_DAYS))


def next_step(inv: dict) -> tuple[str, date] | None:
    """The next reminder this invoice will get: (tone, on which day)."""
    due = date.fromisoformat(inv["due_date"])
    if inv["chase_stage"] < len(STAGES):
        days, tone, _ = STAGES[inv["chase_stage"]]
        return tone, due + timedelta(days=days)
    last = date.fromisoformat(inv["last_reminder_day"]) if inv.get("last_reminder_day") else today()
    return "follow-up", last + timedelta(days=followup_days())


def chase_due() -> list[str]:
    if db.get_setting("chasing_paused", False):
        return []
    clients = db.list_clients()
    skip = {c["email"].lower() for c in clients if c["dont_chase"]}
    skip_names = {c["name"].lower() for c in clients if c["dont_chase"]}
    now_day = today()
    sent = []
    for inv in db.list_invoices(OPEN):
        if inv["client_email"].lower() in skip or (inv["client_name"] or "").lower() in skip_names:
            continue
        overdue = days_overdue(inv)
        if overdue < STAGES[0][0]:
            continue
        stage = inv["chase_stage"]
        if stage < len(STAGES):
            # The reminder that fits today; earlier ones that were missed (the app was asleep,
            # or the demo clock jumped) are skipped, never sent back to back
            target = sum(1 for threshold, _, _ in STAGES if overdue >= threshold)
            if target <= stage:
                continue
            _, tone, subject = STAGES[target - 1]
        else:
            # After the final notice: keep following up every few days until it's paid
            tone, on = next_step(inv)
            if now_day < on:
                continue
            target = stage + 1
            subject = f"Still unpaid: invoice #{{number}} ({target - len(STAGES)} after final notice)"
        # Never two reminders on the same real day, except when the demo clock is moving
        if inv["last_reminder_at"] and inv["last_reminder_at"][:10] == db.now()[:10] \
                and not demo_offset_days():
            continue
        note = write_reminder(inv, tone, overdue, sent_before=stage)
        try:
            pp_invoices.remind(inv["id"], note, subject.format(number=inv["number"]))
        except PayPalError as e:
            print(f"[chasing] remind #{inv['number']}: {e}")
            continue
        fields = {"chase_stage": target, "last_reminder_at": db.now(),
                  "last_reminder_day": now_day.isoformat()}
        if target == len(STAGES):
            fields["final_day"] = now_day.isoformat()
        db.update_invoice(inv["id"], **fields)
        who = inv["client_name"] or inv["client_email"]
        db.save_event({"event_type": f"UPSTREAM.CHASE.{tone.upper().replace('-', '_')}",
                       "resource": {"id": inv["id"]}, "note": note},
                      message=f"Sent a {tone} reminder to {who} for #{inv['number']} "
                              f"(${owed(inv):,.2f}, {overdue} day{'s' if overdue != 1 else ''} "
                              "overdue).", verified=None)
        sent.append(f"{tone} reminder #{inv['number']}")
    return sent + flag_needs_you()


def flag_needs_you() -> list[str]:
    """A week after the final notice, hand the invoice to the owner too (once)."""
    flagged = []
    for inv in db.list_invoices(OPEN):
        if inv["needs_you_at"] or not inv["final_day"]:
            continue
        if (today() - date.fromisoformat(inv["final_day"])).days < NEEDS_YOU_AFTER_DAYS:
            continue
        db.update_invoice(inv["id"], needs_you_at=db.now())
        who = inv["client_name"] or inv["client_email"]
        seen = {1: "has opened it", 0: "has never opened it"}.get(inv.get("viewed"), "")
        db.save_event({"event_type": "UPSTREAM.INVOICE.NEEDS_YOU", "resource": {"id": inv["id"]}},
                      message=f"Needs you: {who} hasn't paid #{inv['number']} "
                              f"(${owed(inv):,.2f}) a week after the final notice"
                              + (f" and {seen}" if seen else "") + ". Reminders continue; "
                              "see the options on the invoice.", verified=None)
        flagged.append(f"needs you #{inv['number']}")
    return flagged


def tick(force_refresh: bool = False) -> list[str]:
    with _lock:
        return refresh_open(force_refresh) + chase_due()
