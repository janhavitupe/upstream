# Upstream

**An AI money agent for independent developers, built on PayPal.**
It pays the open-source projects your work depends on, gets you paid by your clients, runs your retainers, and routes emergency funds straight to the bill instead of to a stranger.

> Money in runs on its own. Money out waits for your yes.

Built for the PayPal *Build What's Next with PayPal and AI* hackathon (submissions close Nov 12, 2026). Full proposal: [`docs/proposal.md`](docs/proposal.md).

## Status

This is a **starter skeleton**. The PayPal integrations and the API routes exist; the AI chat layer and a dashboard come next.

| Module | Priority | What's here | Still to build |
|---|---|---|---|
| 1. Fund your upstream (OSS payouts) | 1 | Repo → dependency parsing → GitHub health signals → scored split → approved PayPal Payouts batch + status | LLM-bounded adjustments, dashboard UI |
| 2. Get paid (invoices) | 2 | Create + send (no approval step), list open, remind, cancel | Chat input, voice-matched reminders, overdue scheduler |
| 3. Fair retainers (subscriptions) | 3 | Product → plan → subscription, pause/resume | Pre-renewal heads-up job |
| 4. Pay the bill, not the person | 4 | Donation order paid directly to the hospital account + capture | Bill upload and AI verification |
| Webhooks | all | Receiver + signature verification + event log | Persisting state |

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
6. Open **http://localhost:8000/docs** to try every endpoint from the browser.

**Quick test without PayPal keys:** in `/docs`, run `POST /api/oss/analyze` with `{"repo": "pallets/flask", "budget": 100}`. It reads Flask's dependencies, scores each one's health on GitHub and proposes a fair split. Add a `GITHUB_TOKEN` to `.env` to avoid GitHub's 60 requests/hour limit.

## Project structure
```
backend/
  main.py              API routes for all four modules
  config.py            settings from .env
  webhooks.py          PayPal webhook receiver + event log
  paypal/              client (OAuth), payouts, invoices, subscriptions, orders, webhooks
  oss/                 github client, manifest parsing, health signals, scoring, analyzer
  agent/explain.py     LLM explanations for each allocation (optional)
scripts/smoke_test.py  checks every PayPal API with your sandbox accounts
docs/proposal.md       full project proposal
```

## PayPal technologies used
Payouts · Invoicing v2 · Catalog Products + Subscriptions · Orders v2 · Webhooks (+ signature verification). All in the sandbox (`api-m.sandbox.paypal.com`).

## AI tools used
- **Claude (Anthropic API)**: writes a plain-language justification for every funding allocation, using only the facts the scoring step produced. Optional; falls back to rule-based text.
- Planned: chat intent parsing for invoices and retainers, voice-matched reminder notes, vision-based bill verification.

## Webhooks (optional)
Expose your local server with `ngrok http 8000`, add `https://<your-ngrok>/webhooks/paypal` in your PayPal app's Webhooks section, and put the Webhook ID in `.env`. Received events appear at `GET /api/events`.

## License
MIT. See [LICENSE](LICENSE).
