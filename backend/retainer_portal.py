"""The client's renewal page (/r/{token}): what's renewing, what they got, pause in one click."""
from __future__ import annotations

from datetime import date
from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from backend import config, db, retainers

router = APIRouter(include_in_schema=False)

STATUS = {"ACTIVE": "Active", "SUSPENDED": "Paused", "APPROVAL_PENDING": "Waiting for your approval",
          "APPROVED": "Approved", "CANCELLED": "Cancelled"}


def _when(iso: str | None) -> str:
    if not iso:
        return "not scheduled"
    d = date.fromisoformat(iso[:10])
    return f"{d:%B} {d.day}, {d.year}"


def _page(r: dict, flash: str = "") -> str:
    who = escape(r["client_name"] or "")
    work = db.work_since(r["id"], retainers._last_heads_up_time_before_latest(r))
    work_html = "".join(f"<li>{escape(w['note'])}</li>" for w in work) or \
        '<li class="muted">Nothing logged yet for this period.</li>'
    token = escape(r["token"])
    if r["status"] == "ACTIVE":
        action = (f'<form method="post" action="/r/{token}/pause"><button class="btn primary">'
                  "Pause the next renewal</button></form>"
                  '<p class="muted small">Pausing stops the next charge. You can resume any time.</p>')
    elif r["status"] == "SUSPENDED":
        action = (f'<form method="post" action="/r/{token}/resume"><button class="btn primary">'
                  "Resume the retainer</button></form>")
    elif r["status"] == "APPROVAL_PENDING" and r["approve_url"]:
        action = (f'<a class="btn primary" href="{escape(r["approve_url"])}">Approve with PayPal'
                  "</a>")
    else:
        action = ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(r['service'])} · {escape(config.INVOICER_NAME)}</title>
<link rel="stylesheet" href="/static/styles.css"></head>
<body><main>
  <header class="top"><div class="brand"><span class="logo" aria-hidden="true">↑</span>
    <div><h1>{escape(config.INVOICER_NAME)}</h1><p class="tagline">Your retainer</p></div></div></header>
  {f'<div class="alert warn">{escape(flash)}</div>' if flash else ''}
  <section class="card">
    <div class="result-head">
      <div><h2>{escape(r['service'])}</h2>
        <p class="muted">{f'For {who} · ' if who else ''}{escape(r['currency'])} {r['amount']:,.2f} a month, charged by PayPal</p></div>
      <span class="status {'SUCCESS' if r['status'] == 'ACTIVE' else 'PENDING'}">{STATUS.get(r['status'], r['status'])}</span>
    </div>
    <p><b>Next renewal:</b> {_when(r['next_billing']) if r['status'] == 'ACTIVE' else '—'}</p>
    <h2 class="section-title">What you got this period</h2>
    <ul>{work_html}</ul>
    {action}
  </section>
  <p class="muted small">No surprise charges: you get a heads-up before every renewal.
    Payments are handled by PayPal.</p>
</main></body></html>"""


def _get(token: str) -> dict:
    r = db.retainer_by_token(token)
    if not r:
        raise HTTPException(404, "This retainer page doesn't exist.")
    return r


@router.get("/r/{token}", response_class=HTMLResponse)
def page(token: str):
    return _page(_get(token))


@router.post("/r/{token}/pause", response_class=HTMLResponse)
def client_pause(token: str):
    r = _get(token)
    try:
        r = retainers.pause(r["id"], by="client")
        return _page(r, f"Paused. {config.INVOICER_NAME} has been told, and there will be no "
                        "renewal until you resume.")
    except retainers.RetainerError as e:
        return _page(r, str(e))


@router.post("/r/{token}/resume", response_class=HTMLResponse)
def client_resume(token: str):
    r = _get(token)
    try:
        r = retainers.resume(r["id"], by="client")
        return _page(r, "Resumed. Thanks!")
    except retainers.RetainerError as e:
        return _page(r, str(e))
