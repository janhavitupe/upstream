"""Public, shareable giving report for one analysis. No emails: projects, people, amounts, why."""
from __future__ import annotations

from datetime import datetime
from html import escape

from backend import config

STATUS_LABEL = {"SUCCESS": "Paid", "PENDING": "Sending", "PROCESSING": "Sending",
                "UNCLAIMED": "Waiting to be claimed", "RETURNED": "Re-queued",
                "FAILED": "Failed", "ONHOLD": "On hold"}


def _money(n: float) -> str:
    return f"${n:,.2f}"


def render(analysis: dict, batch: dict | None) -> str:
    repo = escape(analysis["repo"])
    when = datetime.fromisoformat(analysis["created_at"]).strftime("%B %Y")
    funded = [d for d in analysis["dependencies"] if d.get("amount", 0) > 0]
    idle = [d for d in analysis["dependencies"] if d.get("signals") and not d.get("work_score")]
    total = sum(d["amount"] for d in funded)
    status = {i["sender_item_id"]: i.get("status") for i in (batch or {}).get("items", [])}
    paid = batch is not None
    url = f"{config.APP_BASE_URL}/report/{escape(analysis['analysis_id'])}"
    title = f"{repo} funded {len(funded)} open-source projects"
    summary = (f"{_money(total)} to the maintainers {repo} depends on, split by work done "
               f"times the size of the job.")

    rows = []
    for d in funded:
        s = d.get("signals") or {}
        who = (d.get("intended_for") or {}).get("login")
        who_html = (f'<a href="https://github.com/{escape(who)}">@{escape(who)}</a>'
                    if who else "the maintainers")
        st = status.get(d["package"][:63])
        badge = (f'<span class="status {escape(st or "PENDING")}">'
                 f'{escape(STATUS_LABEL.get(st, st or "Sending"))}</span>') if paid else ""
        work = (f'{s.get("human_commits_last_year", 0)}{"+" if s.get("commits_capped") else ""} '
                f'commits · {s.get("releases_last_year", 0)} releases · '
                f'{round(s.get("code_kb") or 0):,} KB of code')
        carry = (f' · includes {_money(d["carryover"])} re-queued' if d.get("carryover") else "")
        rows.append(f"""
      <li class="dep-card">
        <div class="row">
          <div><span class="pkg-name">{escape(d["package"])}</span>
            <span class="pkg-repo">for {who_html}</span></div>
          <div><strong class="num">{_money(d["amount"])}</strong> {badge}</div>
        </div>
        <p class="why">{escape(d.get("explanation") or "")}</p>
        <div class="facts">{work}{carry}</div>
      </li>""")

    idle_html = ""
    if idle:
        names = ", ".join(escape(d["package"]) for d in idle)
        idle_html = (f'<div class="alert warn">Not funded this time (no commits or releases in '
                     f'the last year): {names}. {repo} still relies on '
                     f'{"it" if len(idle) == 1 else "them"}.</div>')

    sandbox_note = ("Made in the PayPal sandbox: payments went to test accounts standing in for "
                    "the developers named above." if analysis.get("sandbox") else "")
    state = "Paid" if paid else "Proposed (not paid yet)"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="{summary}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{summary}">
  <meta property="og:url" content="{url}">
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <header class="top">
    <div class="brand">
      <span class="logo" aria-hidden="true">↑</span>
      <div><h1>Giving report</h1><p class="tagline">{escape(when)} · {state}</p></div>
    </div>
  </header>
  <main>
    <section class="card">
      <div class="result-head">
        <div>
          <h2>{title}</h2>
          <p class="muted">{summary}</p>
        </div>
        <div class="total-box"><span class="muted">Total</span>
          <strong>{_money(total)}</strong></div>
      </div>
      {idle_html}
      <ul class="dep-cards report">{"".join(rows)}</ul>
    </section>
  </main>
  <footer class="muted small">
    {sandbox_note} Each project is paid for its work in the last year times the size of its
    codebase; an AI read the recent commits and adjusted each share by at most
    {analysis.get("ai_adjust_limit_points", 0):g} points, and a person approved the payout.
    Made with <a href="/">Upstream</a> on PayPal.
  </footer>
</body>
</html>"""
