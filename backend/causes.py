"""Pay the bill, not the person (Module 4): verified medical fundraising.

The organizer uploads the hospital bill. The AI only READS it (provider, patient, date, total,
line items); the CHECKS are plain code, so neither a sloppy model nor a dishonest organizer can
talk past them. Donations open only for verified campaigns and are paid by PayPal straight to the
hospital's account: Upstream never holds the money.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import uuid
from datetime import date, timedelta
from difflib import SequenceMatcher

from backend import config, db
from backend.agent.llm import LLMError, complete, vision
from backend.paypal import orders

MAX_BILL_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_BILL_AGE_DAYS = 365
NAME_MATCH = 0.75  # similarity needed between names (ignoring case, titles and filler words)

EXTRACT_PROMPT = (
    "You are reading a medical bill or hospital invoice. Extract exactly what is printed; never "
    "guess. Return ONLY JSON: {\"looks_like_a_medical_bill\": bool, \"provider\": str|null, "
    "\"patient\": str|null, \"date\": \"YYYY-MM-DD\"|null, \"total\": number|null (the amount "
    "due), \"currency\": str|null, \"line_items\": [{\"name\": str, \"amount\": number}]}")


class CauseError(Exception):
    pass


# ---------- reading the bill ----------

def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Upright, grayscale, contrast-boosted and size-limited: easier and cheaper to read."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)            # phone photos taken sideways
    except Exception:
        raise CauseError("That file isn't a readable image.")
    img = ImageOps.autocontrast(img.convert("L"))     # grayscale + stretch faint print
    img.thumbnail((2000, 2000))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue(), "image/png"


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:5]).strip()
    except Exception:
        raise CauseError("That PDF couldn't be opened.")


def _parse(text: str) -> dict:
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except ValueError:
        raise CauseError("The bill couldn't be read. Try a clearer photo.")
    items = [{"name": str(i.get("name", ""))[:120], "amount": float(i["amount"])}
             for i in (data.get("line_items") or []) if isinstance(i, dict) and _num(i.get("amount"))]
    return {"looks_like_a_medical_bill": bool(data.get("looks_like_a_medical_bill")),
            "provider": (data.get("provider") or None), "patient": (data.get("patient") or None),
            "date": data.get("date") or None, "total": float(data["total"]) if _num(data.get("total")) else None,
            "currency": (data.get("currency") or None), "line_items": items}


def _num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def read_bill(data: bytes, mime: str) -> dict:
    """What the bill says, as extracted by the AI (image: vision model; PDF: its text)."""
    if not config.llm_configured():
        raise CauseError("Bill reading needs the AI (LLM_API_KEY in .env).")
    try:
        if mime == "application/pdf":
            text = pdf_text(data)
            if len(text) < 30:
                raise CauseError("This PDF is a scan with no text. Upload a photo of the bill instead.")
            return _parse(complete(f"{EXTRACT_PROMPT}\n\nBill text:\n{text[:6000]}",
                                   max_tokens=1500, json_mode=True))
        image, image_mime = prepare_image(data)
        return _parse(vision(EXTRACT_PROMPT, image, image_mime, json_mode=True))
    except LLMError as e:
        raise CauseError(f"The AI couldn't read the bill right now ({e}). Try again in a minute.")


# ---------- the checks (code, not AI) ----------

FILLER = {"hospital", "medical", "center", "centre", "clinic", "the", "of", "and", "inc", "llc",
          "ltd", "general", "health", "healthcare", "mr", "mrs", "ms", "dr", "miss"}


def _norm(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    kept = [w for w in words if w not in FILLER]
    return " ".join(kept or words)


def names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y or x.replace(" ", "") == y.replace(" ", ""):
        return True
    if set(x.split()) <= set(y.split()) or set(y.split()) <= set(x.split()):
        return True  # "City General Hospital" vs "City General"
    return SequenceMatcher(None, x, y).ratio() >= NAME_MATCH


def run_checks(c: dict, bill: dict, bill_hash: str) -> list[dict]:
    def check(key, label, ok, detail):
        return {"key": key, "label": label, "ok": bool(ok), "detail": detail}

    out = [check("is_bill", "It's a medical bill", bill["looks_like_a_medical_bill"],
                 "Recognised as a medical bill." if bill["looks_like_a_medical_bill"]
                 else "This doesn't look like a medical bill.")]
    out.append(check("provider", "Hospital matches the campaign",
                     names_match(bill["provider"], c["provider_name"]),
                     f"Bill is from \"{bill['provider'] or 'unknown'}\"; campaign says "
                     f"\"{c['provider_name']}\"."))
    out.append(check("patient", "Patient matches the campaign",
                     names_match(bill["patient"], c["patient_name"]),
                     "The bill's patient matches the person this campaign is for."
                     if names_match(bill["patient"], c["patient_name"])
                     else "The patient on the bill isn't the person this campaign is for."))
    total = bill["total"]
    out.append(check("amount", "Asking for no more than the bill",
                     total is not None and c["goal"] <= total + 0.01,
                     f"Goal {c['currency']} {c['goal']:,.2f}; bill total "
                     + (f"{bill['currency'] or c['currency']} {total:,.2f}." if total is not None
                        else "not found.")))
    try:
        d = date.fromisoformat(bill["date"]) if bill["date"] else None
    except ValueError:
        d = None
    recent = d is not None and date.today() - timedelta(days=MAX_BILL_AGE_DAYS) <= d <= date.today()
    if d is None:
        when = "No readable date on the bill."
    else:
        when = f"Dated {d:%b} {d.day}, {d.year}." + ("" if recent else
                                                     " That's older than a year or in the future.")
    out.append(check("date", "Bill is recent", recent, when))
    items = bill["line_items"]
    if items and total is not None:
        items_total = round(sum(i["amount"] for i in items), 2)
        adds_up = abs(items_total - total) <= max(1.0, total * 0.02)
        out.append(check("adds_up", "Line items add up", adds_up,
                         f"Items total {items_total:,.2f} vs amount due {total:,.2f}."))
    reused = db.campaign_with_bill(bill_hash, exclude_id=c["id"])
    out.append(check("unique", "Bill not used by another campaign", reused is None,
                     "This bill hasn't been used before." if reused is None
                     else "This exact bill is already backing another campaign."))
    return out


def verdict(checks: list[dict]) -> str:
    return "verified" if all(ch["ok"] for ch in checks) else "needs_review"


# ---------- campaigns ----------

def _store_bill(campaign_id: str, data: bytes, mime: str) -> str:
    config.BILLS_DIR.mkdir(parents=True, exist_ok=True)
    ext = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
    path = config.BILLS_DIR / f"{campaign_id}.{ext}"
    path.write_bytes(data)
    return str(path)


def _verify(c: dict, data: bytes, mime: str) -> dict:
    if len(data) > MAX_BILL_BYTES:
        raise CauseError("The bill file is too large (8 MB max).")
    if mime not in IMAGE_TYPES | {"application/pdf"}:
        raise CauseError("Upload the bill as a photo (PNG, JPG, WEBP) or a PDF.")
    bill_hash = hashlib.sha256(data).hexdigest()
    try:
        bill = read_bill(data, mime)
        checks = run_checks(c, bill, bill_hash)
        result = verdict(checks)
    except CauseError as e:
        bill, checks, result = None, [{"key": "readable", "label": "Bill is readable", "ok": False,
                                       "detail": str(e)}], "unreadable"
    return {"bill_path": _store_bill(c["id"], data, mime), "bill_hash": bill_hash,
            "extraction": bill, "checks": checks, "verdict": result}


def create(title: str, story: str, patient_name: str, provider_name: str, provider_email: str,
           goal: float, bill: bytes, mime: str, currency: str = "USD") -> dict:
    if "@" not in provider_email:
        raise CauseError("Enter the hospital's PayPal email: donations are paid straight to it.")
    if goal <= 0 or goal > config.INVOICE_MAX * 4:
        raise CauseError("Enter how much is needed.")
    c = {"id": uuid.uuid4().hex[:12], "token": secrets.token_urlsafe(9), "title": title.strip()[:120],
         "story": story.strip()[:3000], "patient_name": patient_name.strip(),
         "provider_name": provider_name.strip(), "provider_email": provider_email.strip(),
         "goal": round(goal, 2), "currency": currency.upper()[:3], "status": "open"}
    c.update(_verify(c, bill, mime))
    db.save_campaign(c)
    _event("CREATED", c, f"Campaign \"{c['title']}\" created: {_verdict_text(c['verdict'])}.")
    return view(db.get_campaign(c["id"]))


def replace_bill(campaign_id: str, bill: bytes, mime: str) -> dict:
    c = db.get_campaign(campaign_id)
    if not c:
        raise CauseError("Campaign not found.")
    if db.campaign_raised(campaign_id)[1]:
        raise CauseError("Donations have started; the bill can't be changed now.")
    db.update_campaign(campaign_id, **_verify(c, bill, mime))
    c = db.get_campaign(campaign_id)
    _event("REVERIFIED", c, f"New bill for \"{c['title']}\": {_verdict_text(c['verdict'])}.")
    return view(c)


def _verdict_text(v: str) -> str:
    return {"verified": "verified, donations are open", "needs_review": "needs review, donations "
            "stay closed until the bill matches", "unreadable": "the bill couldn't be read"}[v]


def _event(kind: str, c: dict, message: str):
    db.save_event({"event_type": f"UPSTREAM.CAUSE.{kind}", "resource": {"id": c["id"]}},
                  message=message, verified=None)


def initials(name: str) -> str:
    return " ".join(f"{p[0].upper()}." for p in re.findall(r"[A-Za-z]+", name or "")) or "—"


def view(c: dict, public: bool = False) -> dict:
    raised, donors = db.campaign_raised(c["id"])
    out = {k: c[k] for k in ("id", "token", "title", "story", "provider_name", "goal", "currency",
                             "checks", "verdict", "status", "created_at")}
    bill = c["extraction"] or {}
    out.update(raised=raised, donors=donors, remaining=round(max(c["goal"] - raised, 0), 2),
               accepting=accepting(c, raised), patient=initials(c["patient_name"]),
               page_url=f"{config.APP_BASE_URL}/c/{c['token']}",
               bill={"provider": bill.get("provider"), "date": bill.get("date"),
                     "total": bill.get("total"), "currency": bill.get("currency"),
                     "line_items": bill.get("line_items", [])})
    if not public:  # the organizer sees full names and where the money goes
        out.update(patient_name=c["patient_name"], provider_email=c["provider_email"])
    return out


def accepting(c: dict, raised: float | None = None) -> bool:
    raised = db.campaign_raised(c["id"])[0] if raised is None else raised
    return c["verdict"] == "verified" and c["status"] == "open" and raised < c["goal"]


def close(campaign_id: str) -> dict:
    c = db.get_campaign(campaign_id)
    if not c:
        raise CauseError("Campaign not found.")
    db.update_campaign(campaign_id, status="closed")
    _event("CLOSED", c, f"Campaign \"{c['title']}\" closed.")
    return view(db.get_campaign(campaign_id))


# ---------- donations: straight to the hospital ----------

def start_donation(token: str, amount: float) -> str:
    """Create the PayPal order (payee = the hospital). Returns the donor's approval link."""
    c = db.campaign_by_token(token)
    if not c:
        raise CauseError("Campaign not found.")
    raised = db.campaign_raised(c["id"])[0]
    if not accepting(c, raised):
        raise CauseError("This campaign isn't taking donations.")
    remaining = round(c["goal"] - raised, 2)
    if amount < 1:
        raise CauseError("The smallest donation is 1.00.")
    if amount > remaining:
        raise CauseError(f"Only {c['currency']} {remaining:,.2f} is still needed.")
    order = orders.create_donation(round(amount, 2), c["provider_email"],
                                   f"Donation toward {c['title']}"[:127], currency=c["currency"],
                                   custom_id=c["id"])
    db.add_donation(order["id"], c["id"], round(amount, 2))
    return orders.approve_link(order)


def complete_donation(order_id: str, capture: bool = True) -> dict | None:
    """Capture an approved order (or record a capture PayPal reported). Counted once."""
    d = db.get_donation(order_id)
    if not d:
        return None
    paid_to = None
    if capture and d["status"] != "COMPLETED":
        result = orders.capture(order_id)
        unit = (result.get("purchase_units") or [{}])[0]
        paid_to = (unit.get("payee") or {}).get("email_address")
        if result.get("status") != "COMPLETED":
            return {**d, "status": result.get("status"), "paid_to": paid_to}
    c = db.get_campaign(d["campaign_id"])
    if db.complete_donation(order_id):
        raised, donors = db.campaign_raised(c["id"])
        _event("DONATION", c, f"{c['currency']} {d['amount']:,.2f} donated to \"{c['title']}\", "
                              f"paid directly to {c['provider_name']}. {c['currency']} "
                              f"{raised:,.2f} of {c['goal']:,.2f} raised.")
    return {**db.get_donation(order_id), "paid_to": paid_to or c["provider_email"],
            "campaign": view(c, public=True)}
