# Upstream

**An AI money agent for independent developers, built on PayPal.**
It pays your team, gets you paid by your clients, runs your retainers, and routes emergency funds straight to the bill instead of to a stranger.

> Money in runs on its own. Money out waits for your yes.

Built for the PayPal *Build What's Next with PayPal and AI* hackathon (submissions close Nov 12, 2026). Full proposal: [`docs/proposal.md`](docs/proposal.md).

## Status

All four modules work end to end from the dashboard at `/`.

| Module | Priority | What's here | Still to build |
|---|---|---|---|
| 1. Pay your team (payroll) | 1 | **Done.** Pick a repo you own, an amount, and when: every day, week or month at a time you choose, in your time zone. Each payday the top contributors (you excluded) are paid by work done (commits × lines changed) with an AI review of their commits (±5 pts). You approve the schedule once; a heads-up preview comes before each payday and can be skipped or edited, otherwise it pays automatically. In the sandbox each person is paid at one of your sandbox accounts (`DEMO_TEAM_EMAILS`, in turn) unless you enter their own email. Daily check via GitHub Actions (`.github/workflows/payroll-tick.yml`) | GitHub sign-in to prove repo ownership; contributors adding their own PayPal email; persistent database on Render |
| 2. Get paid (invoices) | 2 | **Chat + chasing done.** "Bill Acme $500 for the API work, due next Friday" → the AI (tool calling) creates and sends a PayPal invoice at once, with Undo and the client's pay page. Invoices are chased automatically until paid (a friendly reminder every day until the due date, one the day after it, then every 3 days: firm, urgent and final, each AI-written note firmer than the last; missed reminders skipped, never sent back to back), with a global pause and per-client opt-out. Past 30 days overdue the invoice also **needs you**: did the client open it, a ready-to-send personal message, let them pay in parts (PayPal re-sends), a paper trail, write off. payments are detected (webhook or PayPal check) and stop chasing, with a note on how much to set aside for tax. Sandbox demo clock to show a month in a minute | Reminders in your own writing style: **done** (paste 2–5 messages, preview generic vs your voice, tone level always wins) |
| 3. Fair retainers (subscriptions) | 3 | **Done.** "Set up a retainer for Acme, $300/month" by chat → PayPal subscription + approval link; work log ("log for Acme: shipped X"); 3 days before each renewal an AI heads-up (what's renewing, amount, what was delivered) with the client's page `/r/{code}` to pause in one click; payments, pauses and approvals picked up from PayPal | Heads-up email needs SMTP in `.env`; without it you copy/send it from the card |
| 4. Pay the bill, not the person | 4 | **Done.** Upload a hospital bill (photo or PDF): a vision AI reads it, then **code** checks 7 things (it's a medical bill, hospital and patient match, goal ≤ bill total, recent, line items add up, bill not reused). Only verified campaigns take donations; PayPal Orders pays the hospital directly (payee = hospital), capped at what's still needed. Public page `/c/{code}` shows the checks with the patient as initials | Provider identity verification (e.g. checking the hospital's PayPal is genuinely the hospital) |
| Webhooks + storage | all | Receiver + signature verification. SQLite locally; **PostgreSQL** (e.g. free Neon) when `DATABASE_URL` is set, so data survives deploys ([docs/database.md](docs/database.md)). The whole test suite passes on both | |

**Testing guide tab:** step-by-step tests for every feature, live ticks showing which tasks have been done, sample real and fake bills, and the sandbox logins. Logins are never committed: set `SANDBOX_LOGINS` (`email=password,email=password`) in `.env` and on the host. They are only shown when `PAYPAL_BASE` is the sandbox.

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

**Quick test without PayPal keys:** on **Pay your team**, preview a split for any public repo (e.g. `expressjs/express`). It reads the last month's commits on GitHub and proposes how to split your monthly total. Add a `GITHUB_TOKEN` to `.env` to avoid GitHub's 60 requests/hour limit.

## Tests and CI

```bash
pip install -r requirements-dev.txt
python -m pytest                              # 130 tests, every feature, offline (~15 s)
UPSTREAM_E2E=1 python -m pytest -m e2e        # 9 end-to-end tests against the real PayPal sandbox
```

Every feature has tests: the split and the AI's ±5-point limit, team payroll (contributor work,
split, heads-up, payday, never twice, who has been paid), invoicing chat and every tool,
chasing until paid, Needs you, your writing style, retainers (heads-ups, client page,
pause), verified causes (bill reading, every check, donations to the hospital), webhooks, database upgrades and the AI client's retries. Offline tests use an in-memory
fake PayPal, AI and GitHub (`tests/fakes.py`). A [`Jenkinsfile`](Jenkinsfile) runs the unit tests
on every push and the end-to-end tests nightly; setup in [`docs/jenkins.md`](docs/jenkins.md).

## Project structure
```
backend/
  main.py              API routes for all four modules
  config.py            settings from .env
  webhooks.py          PayPal webhook receiver; payout events update stored status
  db.py                SQLite / PostgreSQL storage
  github.py            GitHub client (commits and line stats for payroll)
  split.py             exact-to-the-cent split + the bounded AI adjustment
  payroll.py           monthly team payroll: contributor work, split, schedule, scheduler
  payroll_api.py       payroll API routes
  chasing.py           automatic overdue reminders + payment detection
  scheduler.py         one timer for payroll, chasing and retainers (POST /api/tick)
  retainers.py         monthly retainers: PayPal subscriptions, heads-ups, pause/resume
  retainer_portal.py   the client's renewal page (/r/{code})
  causes.py            verified causes: bill reading, checks, donations to the hospital
  causes_api.py        causes routes + the public campaign page (/c/{code})
  paypal/              client (OAuth), payouts, invoices, subscriptions, orders, webhooks
  agent/explain.py     AI review of each person's pay (optional)
  agent/billing.py     billing chat agent: tools for PayPal invoicing
  getpaid_api.py       Get paid routes (chat, invoices, clients)
frontend/              dashboard (plain HTML/JS; card view on phones)
scripts/smoke_test.py  checks every PayPal API with your sandbox accounts
docs/proposal.md       full project proposal
```

## PayPal technologies used
Payouts · Invoicing v2 · Catalog Products + Subscriptions · Orders v2 · Webhooks (+ signature verification). All in the sandbox (`api-m.sandbox.paypal.com`).

## AI tools used
- **gpt-oss-120b on Groq** (any OpenAI-compatible API, set in `.env`): reviews each team member's commits and adjusts their share by ±5 points at most, with a reason; runs the billing chat (tool calling) for invoices and retainers; writes reminders in your own style and retainer heads-ups. Optional; falls back to rule-based text.
- **A vision model** (`LLM_VISION_MODEL`): reads hospital bills for verified causes. Plain code makes every check.

## Webhooks (optional)
Expose your local server with `ngrok http 8000`, add `https://<your-ngrok>/webhooks/paypal` in your PayPal app's Webhooks section, and put the Webhook ID in `.env`. Received events appear at `GET /api/events`.

## License
MIT. See [LICENSE](LICENSE).
