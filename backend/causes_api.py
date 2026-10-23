"""Routes for verified causes (Module 4): the organizer's API and the public campaign page."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from backend import causes, config, db

router = APIRouter()


def _mime(upload: UploadFile) -> str:
    name = (upload.filename or "").lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    for ext, mime in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
                      (".webp", "image/webp")):
        if name.endswith(ext):
            return mime
    return upload.content_type or "application/octet-stream"


@router.post("/api/causes", tags=["4 · Verified causes"])
async def create_campaign(title: str = Form(...), story: str = Form(...),
                          patient_name: str = Form(...), provider_name: str = Form(...),
                          provider_email: str = Form(...), goal: float = Form(...),
                          bill: UploadFile = File(...)):
    """Create a campaign from a hospital bill. The AI reads it, code checks it."""
    try:
        return causes.create(title, story, patient_name, provider_name, provider_email, goal,
                             await bill.read(), _mime(bill))
    except causes.CauseError as e:
        raise HTTPException(400, str(e))


@router.get("/api/causes", tags=["4 · Verified causes"])
def list_campaigns():
    return [causes.view(c) for c in db.list_campaigns()]


def _get(campaign_id: str) -> dict:
    c = db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found.")
    return c


@router.get("/api/causes/{campaign_id}", tags=["4 · Verified causes"])
def get_campaign(campaign_id: str):
    return causes.view(_get(campaign_id))


@router.post("/api/causes/{campaign_id}/bill", tags=["4 · Verified causes"])
async def replace_bill(campaign_id: str, bill: UploadFile = File(...)):
    """Upload a corrected bill; it's read and checked again."""
    _get(campaign_id)
    try:
        return causes.replace_bill(campaign_id, await bill.read(), _mime(bill))
    except causes.CauseError as e:
        raise HTTPException(409, str(e))


@router.post("/api/causes/{campaign_id}/close", tags=["4 · Verified causes"])
def close_campaign(campaign_id: str):
    _get(campaign_id)
    return causes.close(campaign_id)


# ---------- the public page ----------

def _money(c: dict, n: float) -> str:
    return f"{c['currency']} {n:,.2f}"


def page_html(c: dict, flash: str = "", thanks: float | None = None) -> str:
    v = causes.view(c, public=True)
    pct = min(100, round(100 * v["raised"] / v["goal"])) if v["goal"] else 0
    badge = {"verified": ("SUCCESS", "Bill verified"), "needs_review": ("PENDING", "Bill under review"),
             "unreadable": ("PENDING", "Bill under review")}[v["verdict"]]
    checks = "".join(f'<li>{"✅" if ch["ok"] else "⚠️"} <b>{escape(ch["label"])}</b> '
                     f'<span class="muted small">{escape(ch["detail"])}</span></li>'
                     for ch in v["checks"])
    b = v["bill"]
    items = "".join(f"<li>{escape(i['name'])} <span class='num'>{_money(c, i['amount'])}</span></li>"
                    for i in b["line_items"])
    if v["accepting"]:
        donate = f"""
      <form method="post" action="/c/{escape(c['token'])}/donate" class="donate-form">
        <label class="field"><span>Donation ({escape(c['currency'])})</span>
          <input name="amount" type="number" min="1" max="{v['remaining']}" step="1" value="{min(25, int(v['remaining']) or 1)}" required></label>
        <button class="btn primary">Donate with PayPal</button>
      </form>
      <p class="muted small">Your payment goes <b>directly to {escape(c['provider_name'])}</b> through
        PayPal. Nobody else can touch it.</p>"""
    elif c["status"] == "closed":
        donate = '<p class="muted">This campaign is closed.</p>'
    elif v["raised"] >= v["goal"]:
        donate = '<p><b>Fully funded. Thank you!</b></p>'
    else:
        donate = ('<p class="muted">Donations open once the bill has been verified against the '
                  "campaign details.</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(c['title'])}</title>
<meta property="og:title" content="{escape(c['title'])}">
<meta property="og:description" content="{escape(c['story'][:150])}">
<link rel="stylesheet" href="/static/styles.css"></head>
<body><main>
  <header class="top"><div class="brand"><span class="logo" aria-hidden="true">↑</span>
    <div><h1>Verified cause</h1><p class="tagline">Pay the bill, not the person</p></div></div></header>
  {f'<div class="alert warn">{escape(flash)}</div>' if flash else ''}
  {f'<div class="alert good">Thank you! Your {_money(c, thanks)} was paid directly to {escape(c["provider_name"])}.</div>' if thanks else ''}
  <section class="card">
    <div class="result-head"><div><h2>{escape(c['title'])}</h2>
      <p class="muted">For {escape(v['patient'])} · bill from {escape(c['provider_name'])}</p></div>
      <span class="status {badge[0]}">{badge[1]}</span></div>
    <p>{escape(c['story'])}</p>
    <div class="progress-bar" aria-label="{pct}% funded"><span style="width:{pct}%"></span></div>
    <p><b>{_money(c, v['raised'])}</b> raised of {_money(c, v['goal'])} · {v['donors']} donation{'s' if v['donors'] != 1 else ''}</p>
    {donate}
  </section>
  <section class="card">
    <h2>The bill, checked</h2>
    <p class="muted small">An AI read the uploaded bill; these checks are done by code. Personal
      details are hidden: the patient's name is shown as initials and the bill itself isn't public.</p>
    <ul class="checks">{checks}</ul>
    {f'''<p><b>{escape(b["provider"] or "")}</b>{f" · {escape(b['date'])}" if b["date"] else ""}
      {f" · amount due {_money(c, b['total'])}" if b["total"] is not None else ""}</p>
    <ul>{items}</ul>''' if b["provider"] or b["total"] else ''}
  </section>
  <p class="muted small">Upstream never holds donations: PayPal pays the provider directly.</p>
</main></body></html>"""


@router.get("/c/{token}", include_in_schema=False, response_class=HTMLResponse)
def public_page(token: str, thanks: float | None = None):
    c = db.campaign_by_token(token)
    if not c:
        raise HTTPException(404, "Campaign not found.")
    return page_html(c, thanks=thanks)


@router.post("/c/{token}/donate", include_in_schema=False)
def donate(token: str, amount: float = Form(...)):
    c = db.campaign_by_token(token)
    if not c:
        raise HTTPException(404, "Campaign not found.")
    try:
        return RedirectResponse(causes.start_donation(token, amount), status_code=303)
    except causes.CauseError as e:
        return HTMLResponse(page_html(c, flash=str(e)), status_code=400)


def donation_return(order_id: str) -> RedirectResponse | None:
    """PayPal sends the donor back here after approving; capture and show the campaign."""
    if not db.get_donation(order_id):
        return None
    result = causes.complete_donation(order_id)
    token = result["campaign"]["token"]
    if result["status"] != "COMPLETED":
        return RedirectResponse(f"/c/{token}", status_code=303)
    return RedirectResponse(f"/c/{token}?thanks={result['amount']}", status_code=303)
