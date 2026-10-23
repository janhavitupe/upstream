# Upstream

**An AI money agent for independent developers, built on PayPal.**
It pays the open-source projects your work depends on, gets you paid by your clients, runs your retainers, and routes emergency funds straight to the bill instead of to a stranger.

> Money in runs on its own. Money out waits for your yes.

Built for the PayPal *Build What's Next with PayPal and AI* hackathon (submissions close Nov 12, 2026). Full proposal: [`docs/proposal.md`](docs/proposal.md).

## Status

Module 1 works end to end from the dashboard at `/`. The PayPal integrations for Modules 2–4 exist as API routes; their AI layer and screens come next.

| Module | Priority | What's here | Still to build |
|---|---|---|---|
| 1. Fund your upstream (OSS payouts) | 1 | **Done.** Dashboard: repo → dependencies → pay for work done × code size (idle projects get $0 and a warning) → AI reads recent commits and adjusts each share by ±5 pts max, with reasons → edit → approve → PayPal Payouts batch → live status; unclaimed/returned payouts re-queued to the next payout; shareable giving report at `/report/{id}` | Maintainer sign-up to receive payouts directly |
| 1b. Pay your team (monthly payroll) | 1 | **Done.** Pick a repo you own, a monthly total and a pay day. Each month the top contributors (you excluded) are paid by work done (commits × lines changed) with an AI review of their commits (±5 pts). You approve the schedule once; a heads-up preview comes before each payday and can be skipped or edited, otherwise it pays automatically. Daily check via GitHub Actions (`.github/workflows/payroll-tick.yml`) | GitHub sign-in to prove repo ownership; contributors adding their own PayPal email; persistent database on Render |
| 2. Get paid (invoices) | 2 | **Chat + chasing done.** "Bill Acme $500 for the API work, due next Friday" → the AI (tool calling) creates and sends a PayPal invoice at once, with Undo and the client's pay page. Overdue invoices are chased automatically until paid (friendly → firm → final, then a follow-up every N days; AI-written notes; missed stages skipped), with a global pause and per-client opt-out. A week after the final notice the invoice also **needs you**: did the client open it, a ready-to-send personal message, let them pay in parts (PayPal re-sends), a paper trail, write off. payments are detected (webhook or PayPal check) and stop chasing, with a tax / give-back note. Sandbox demo clock to show a month in a minute | Reminders in your own writing style: **done** (paste 2–5 messages, preview generic vs your voice, tone level always wins) |
| 3. Fair retainers (subscriptions) | 3 | Product → plan → subscription, pause/resume | Pre-renewal heads-up job |
| 4. Pay the bill, not the person | 4 | Donation order paid directly to the hospital account + capture | Bill upload and AI verification |
| Webhooks + storage | all | Receiver + signature verification; SQLite (`data/upstream.db`) stores analyses, payout batches/items and events | Invoice and chase state (Phase 2) |

## Run it (VS Code)

1. Open this folder in VS Code and install the **Python** extension if prompted.
2. Create a virtual environment and install dependencies (terminal: `` Ctrl+` ``):
   ```bash
   python -m venv .venv
   # Windows:   .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and paste your PayPal sandbox **Client ID** and **Secret**.
4. Check PayPal access: run the **"PayPal smoke test"** launch config (or `python scripts/smoke_test.py`). Every line should say PASS.
5. Start the API: press **F5** → **"Run Upstream API"** (or `uvicorn backend.main:app --reload`).
6. Open **http://localhost:8000** for the dashboard, or **http://localhost:8000/docs** to try every endpoint.

**Quick test without PayPal keys:** on the dashboard, analyze `pallets/flask` (or in `/docs`, run `POST /api/oss/analyze` with `{"repo": "pallets/flask", "budget": 100}`). It reads Flask's dependencies, scores each one's health on GitHub and proposes a fair split. Add a `GITHUB_TOKEN` to `.env` to avoid GitHub's 60 requests/hour limit.

## Project structure
```
backend/
  main.py              API routes for all four modules
  config.py            settings from .env
  webhooks.py          PayPal webhook receiver; payout events update stored status
  db.py                SQLite: analyses, payout batches + items, carry-over, webhook events
  requeue.py           unclaimed/returned payouts -> carry-over for the next payout
  report.py            public giving report page
  payroll.py           monthly team payroll: contributor work, split, schedule, scheduler
  payroll_api.py       payroll API routes
  chasing.py           automatic overdue reminders + payment detection
  scheduler.py         one timer for payroll and chasing (POST /api/tick)
frontend/              dashboard (plain HTML/JS + AG Grid; card view on phones)
  paypal/              client (OAuth), payouts, invoices, subscriptions, orders, webhooks
  oss/                 github client, manifest parsing, health signals, scoring, analyzer
  agent/explain.py     LLM explanations for each allocation (optional)
  agent/billing.py     billing chat agent: tools for PayPal invoicing
  getpaid_api.py       Get paid routes (chat, invoices, clients)
scripts/smoke_test.py  checks every PayPal API with your sandbox accounts
docs/proposal.md       full project proposal
```

## PayPal technologies used
Payouts · Invoicing v2 · Catalog Products + Subscriptions · Orders v2 · Webhooks (+ signature verification). All in the sandbox (`api-m.sandbox.paypal.com`).

## AI tools used
- **gpt-oss-120b on Groq** (any OpenAI-compatible API, set in `.env`): writes a plain-language justification for every funding allocation, using only the facts the scoring step produced. Optional; falls back to rule-based text.
- Planned: chat intent parsing for invoices and retainers, voice-matched reminder notes, vision-based bill verification.

## Webhooks (optional)
Expose your local server with `ngrok http 8000`, add `https://<your-ngrok>/webhooks/paypal` in your PayPal app's Webhooks section, and put the Webhook ID in `.env`. Received events appear at `GET /api/events`.

## License
MIT. See [LICENSE](LICENSE).
