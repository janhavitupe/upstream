# Upstream: Project Proposal
**Hackathon:** Build What's Next with PayPal and AI
**Submission deadline:** Nov 12, 2026, 3:00 PM EST (Nov 13, 1:30 AM IST)
**Builder:** Janhavi Tupe ([@janhavitupe](https://github.com/janhavitupe))
**Working name:** *Upstream* (placeholder)

---

## 1. One-line pitch
**Upstream is an AI money agent for independent developers. It pays the open-source projects your work depends on, gets you paid by your clients, runs your retainers, and routes emergency funds straight to the bill instead of to a stranger.**

## 2. The problem
Independent developers sit in the middle of a broken money loop:

| Pain | Evidence |
|---|---|
| **Open-source maintainers go unpaid** | Tidelift 2024: 60% of maintainers are unpaid, almost 60% have quit or considered quitting, almost half are solo maintainers. Funding is fragmented, which one developer described as a "checkout conversion problem." |
| **Freelancers chase late invoices** | Chasing payments becomes "an unofficial second job." Reminder apps fail because people dread writing the follow-up, not because they forget. QuickBooks 2026: businesses with unpaid invoices are owed $17.7K on average. |
| **Mobile invoicing got worse** | A PayPal app review with 334 "helpful" votes says proper invoice creation was removed from mobile, forcing business users onto desktop. |
| **Subscriptions and retainers breed distrust** | 70% of people have forgotten to cancel a free trial (2026 survey); Americans waste ~$252/year on forgotten subscriptions (CNET). Clients who feel ambushed by renewals ask for refunds and leave. |
| **Emergency crowdfunding is hard to trust** | GoFundMe can't require proof of diagnosis (US health privacy rules) and advises donating only to people you know. |

## 3. Who it's for
**Primary user:** freelance and indie developers, plus small dev studios (1–10 people), who earn from clients and build on open source.
**Secondary users:** their clients (invoice and retainer recipients), OSS maintainers (payout recipients), and community donors.

## 4. The product: four modules, one engine

### Shared engine: "Read → Reason → Act"
Every module follows the same loop:
1. **Read:** the AI ingests something (a chat message, a dependency file, a subscription schedule, a hospital bill).
2. **Reason:** it checks the input, decides, and writes a plain-language explanation.
3. **Act:** PayPal executes. Money **going out** always needs the user's approval. Money **coming in** (invoices, reminders) is autonomous.

### Module 1 (Priority 1): Fund Your Upstream (OSS payouts)
- The user connects a GitHub repo, or several.
- The AI parses dependency files (`requirements.txt`, `pyproject.toml`, `package.json`) and pulls GitHub signals for each dependency:
  - number of active maintainers (bus factor)
  - release cadence and last commit date
  - open issues, especially security-labelled ones
  - how many of the user's files import it (how critical it is)
  - `FUNDING.yml` and the maintainer's public contact
- It builds a **dependency health map** and proposes a **fair split** of a monthly budget, with a written reason for each allocation. Example: *"Solo maintainer, last release 14 months ago, imported in 9 of your files → 22%."*
- The user can adjust the sliders, then approves.
- **One PayPal Payouts batch** pays every maintainer at once. Maintainers without PayPal accounts receive a claim email.
- Live payout status (paid / pending / unclaimed / returned), with unclaimed items auto-cancelled and re-queued for next month.
- A monthly "giving report" the user can share publicly or attach to an Open Source Pledge profile.

**Scoring design:** a deterministic base score (criticality × health risk) keeps the numbers explainable. The LLM then adjusts within bounds and writes the justification. Judges see real reasoning, not a black box.

### Module 2 (Priority 2): Get Paid (chat-first invoicing + chasing)
- **Invoice by chat on mobile:** *"Bill Acme $500 for the API integration, due in 14 days."* The AI extracts client, line items, amount, currency and due date, creates the invoice, and **sends it immediately (no approval step)**. The user gets a confirmation card with an undo/cancel option.
- **Autonomous chasing:** the agent watches invoice status. When an invoice is overdue it sends escalating PayPal reminders (friendly → firm → final notice), each with a **note written in the user's own voice**. The style is learned from 3–5 sample messages the user provides once.
- A global "pause chasing" switch and a per-client "don't chase" flag.
- **Payment-landed moment:** when an invoice is paid, the agent suggests a tax set-aside amount and can offer to route a % of that income to Module 1 ("give back 2% of client income upstream").

### Module 3 (Priority 3): Fair Retainers (subscriptions)
- Recurring clients go on a PayPal subscription plan created by chat (*"Retainer for Acme, $300/month, starting Nov 1"*).
- **Pre-renewal heads-up:** a few days before each billing date, the AI sends the client a clear notice with what's renewing, the amount, and what was delivered last cycle. The client can ask to pause.
- A pause request suspends the subscription; resume reactivates it.
- This turns the forgotten-subscription problem into a trust feature on the merchant side, where it belongs.

### Module 4 (Priority 4): Pay the Bill, Not the Person (verified crowdfunding)
- Someone in the community (for example, a maintainer facing a medical emergency) opens a campaign and uploads the hospital or pharmacy bill.
- **AI bill verification:** image preprocessing (OpenCV: deskew, denoise) plus vision-LLM extraction of provider name, amount, date and patient name. These are checked against the campaign details, and mismatches are flagged.
- Donations are checked out with **PayPal Orders, with the hospital's merchant account as the payee**, so money goes **directly to the provider**. Upstream never holds funds.
- A public campaign page shows the verified bill summary (sensitive details redacted), amount raised against the bill total, and where each payment went.

## 5. PayPal APIs needed
All calls go to sandbox: `https://api-m.sandbox.paypal.com`.

### Authentication (all modules)
| Purpose | Endpoint |
|---|---|
| Get access token | `POST /v1/oauth2/token` (client credentials) |

### Module 1: Payouts
| Purpose | Endpoint |
|---|---|
| Create batch payout (all maintainers in one call) | `POST /v1/payments/payouts` |
| Batch status + item list | `GET /v1/payments/payouts/{payout_batch_id}` |
| Single item status | `GET /v1/payments/payouts-item/{payout_item_id}` |
| Cancel an unclaimed item | `POST /v1/payments/payouts-item/{payout_item_id}/cancel` |

Notes: up to 15,000 items per call; all items in one batch must use the same currency; `sender_batch_id` must be unique (reused IDs within 30 days are rejected, which gives you idempotency for free). Production use requires requesting Payouts access, but sandbox works.

### Module 2: Invoicing v2
| Purpose | Endpoint |
|---|---|
| Next invoice number | `POST /v2/invoicing/generate-next-invoice-number` |
| Create draft invoice | `POST /v2/invoicing/invoices` |
| Send invoice | `POST /v2/invoicing/invoices/{invoice_id}/send` |
| Send reminder (AI-written note) | `POST /v2/invoicing/invoices/{invoice_id}/remind` |
| Get invoice / status | `GET /v2/invoicing/invoices/{invoice_id}` |
| Find overdue / unpaid invoices | `POST /v2/invoicing/search-invoices` |
| List invoices | `GET /v2/invoicing/invoices` |
| Cancel a sent invoice (undo) | `POST /v2/invoicing/invoices/{invoice_id}/cancel` |
| QR code for in-person payment | `POST /v2/invoicing/invoices/{invoice_id}/generate-qr-code` |

### Module 3: Catalog + Subscriptions
| Purpose | Endpoint |
|---|---|
| Create product (the service) | `POST /v1/catalogs/products` |
| Create billing plan (price, interval) | `POST /v1/billing/plans` |
| Create subscription (returns client approval link) | `POST /v1/billing/subscriptions` |
| Get subscription (next billing time) | `GET /v1/billing/subscriptions/{id}` |
| Pause | `POST /v1/billing/subscriptions/{id}/suspend` |
| Resume | `POST /v1/billing/subscriptions/{id}/activate` |
| Cancel | `POST /v1/billing/subscriptions/{id}/cancel` |
| Billing history | `GET /v1/billing/subscriptions/{id}/transactions` |

### Module 4: Orders v2 + JS SDK
| Purpose | Endpoint |
|---|---|
| Create donation order (`purchase_units[].payee` = hospital merchant) | `POST /v2/checkout/orders` |
| Capture donation | `POST /v2/checkout/orders/{id}/capture` |
| Donate button on campaign page | PayPal JavaScript SDK (Buttons) |

### Webhooks (all modules)
| Purpose | Endpoint / events |
|---|---|
| Register webhook | `POST /v1/notifications/webhooks` |
| Verify signature | `POST /v1/notifications/verify-webhook-signature` |
| Payouts events | `PAYMENT.PAYOUTSBATCH.SUCCESS`, `PAYMENT.PAYOUTS-ITEM.SUCCEEDED`, `PAYMENT.PAYOUTS-ITEM.UNCLAIMED`, `PAYMENT.PAYOUTS-ITEM.RETURNED` |
| Invoicing events | `INVOICING.INVOICE.PAID`, `INVOICING.INVOICE.CANCELLED` |
| Subscription events | `BILLING.SUBSCRIPTION.ACTIVATED`, `BILLING.SUBSCRIPTION.SUSPENDED`, `BILLING.SUBSCRIPTION.CANCELLED`, `PAYMENT.SALE.COMPLETED` (each renewal charge) |
| Donation events | `PAYMENT.CAPTURE.COMPLETED` |

### Optional accelerator
**PayPal Agent Toolkit** (`paypal/agent-toolkit`, Python supported). It ships pre-built function-calling tools for invoices, orders and subscriptions, so the LLM can call PayPal directly in Modules 2–3. Payouts isn't in the toolkit, so wrap it as your own tool.

> Verify exact request fields against developer.paypal.com before building; endpoint paths above reflect the current REST docs.

## 6. Other APIs and tools
| Tool | Use |
|---|---|
| **LLM with tool calling** (e.g. Claude or GPT) | Chat intent parsing, allocation reasoning, voice-matched reminder notes, renewal notices |
| **Vision LLM + OpenCV** | Bill preprocessing and field extraction (Module 4) |
| **GitHub REST API** | Repo contents, dependency files, contributors, releases, issues, `FUNDING.yml` |
| **PyPI / npm registry APIs** | Map package names to source repos |
| **FastAPI** | Backend + webhook receiver |
| **SQLite / Postgres** | Invoices, chase state, allocations, payout logs |
| **React (mobile-first) or Next.js** | Chat UI + dashboard |
| **Render** (sponsor) | Hosted demo URL + scheduled jobs (overdue checks, renewal notices) |

## 7. Architecture
```
 Mobile chat / web dashboard
            │
        FastAPI backend ──── Scheduler (Render cron): overdue scan, renewal notices, monthly OSS run
            │
      Agent layer (LLM + tools)
   ┌────────┼──────────┬───────────────┐
 GitHub   PayPal     PayPal          PayPal
 + PyPI   Payouts    Invoicing /     Orders
 /npm                Subscriptions   (payee = hospital)
            │
   Webhook receiver ← PayPal events → status updates + "payment landed" messages
```

## 8. Trust and safety
| Action | Autonomy |
|---|---|
| Create + send invoice | **Autonomous** (undo via cancel) |
| Overdue reminders | **Autonomous**, with global pause and per-client opt-out |
| Renewal heads-up to clients | **Autonomous** |
| OSS payout batch (money out) | **Requires approval**, with monthly cap and per-maintainer cap |
| Donation routing | Donor approves their own payment; funds go only to a verified provider |

Every AI decision is logged with its reasoning, and nothing is hidden.

## 9. How it maps to the judging criteria
| Criterion | How Upstream scores |
|---|---|
| **Technological implementation** | Five PayPal API families (Payouts, Invoicing, Subscriptions, Orders, Webhooks), webhook-driven state, LLM tool-calling, vision-based document verification, GitHub dependency analysis |
| **Design** | One engine (Read → Reason → Act) and one consistent approval model across every module; mobile-first chat plus a clear dashboard |
| **Potential impact** | Each module rests on a cited, specific pain point; a single audience (independent developers) connects them |
| **Innovation** | AI-decided, explained OSS funding splits; reminders in the user's own voice; a "pay the bill, not the person" donation flow |
| **Presentation** | A demo that runs end to end on a real repo, with sandbox money visibly moving |

## 10. Build plan (Oct 21 to Nov 12)
| Dates | Goal |
|---|---|
| **Oct 21–23** | PayPal sandbox accounts (business + several personal for maintainers/clients), OAuth, repo setup with MIT license, FastAPI skeleton, webhook receiver |
| **Oct 24–30** | **Module 1:** dependency parsing, GitHub signals, scoring + LLM justification, allocation UI, Payouts batch + status tracking |
| **Oct 31–Nov 4** | **Module 2:** chat → invoice create/send, overdue scan, voice-matched reminders, payment-landed message |
| **Nov 5–7** | **Module 3:** product/plan/subscription by chat, renewal notice job, pause/resume |
| **Nov 8–9** | **Module 4:** bill upload + extraction + match check, Orders donation to hospital payee |
| **Nov 10** | Deploy to Render, end-to-end test, README with setup steps |
| **Nov 11** | Record and edit the video, write the submission text |
| **Nov 12** | Buffer; submit before 3:00 PM EST |

**Cut line:** if you fall behind, Module 4 shrinks to bill verification only, then Module 3 shrinks to the renewal notice only. Modules 1 and 2 must be fully working.

## 11. Demo video (under 3 minutes)
| Time | Scene |
|---|---|
| 0:00–0:20 | Problem: maintainer statistics plus the "second job" freelancer quote |
| 0:20–1:15 | **Module 1:** paste a real repo, see the health map, AI split with reasons, adjust, approve, watch the Payouts batch succeed in sandbox |
| 1:15–1:55 | **Module 2:** on a phone, "Bill Acme $500…" sends the invoice; jump to overdue; the reminder goes out in your voice; client pays; "payment landed" + give-back prompt |
| 1:55–2:20 | **Module 3:** create a retainer by chat; client receives the renewal heads-up; pause |
| 2:20–2:45 | **Module 4:** upload a bill, AI verifies the match, donate, money lands at the hospital account |
| 2:45–3:00 | Close: "Money in runs on its own. Money out waits for your yes." |

## 12. Submission checklist
- [ ] Public GitHub repo with all source, assets and setup instructions
- [ ] License file (MIT) visible in the repo's About section
- [ ] Hosted demo URL (Render) **and/or** complete run instructions
- [ ] Text description of features
- [ ] "Tools used" section: which AI models and how each is used
- [ ] Public YouTube video under 3 minutes, showing it working on a phone and a laptop

## 13. Risks
| Risk | Mitigation |
|---|---|
| Scope (four modules in three weeks) | Strict priority order and the cut line above |
| Maintainer PayPal emails unknown | Use `FUNDING.yml` / public emails; in the demo, map maintainers to sandbox accounts and say so clearly |
| Bill verification can be fooled | Present it as a match check plus flagged mismatches, not proof; note provider verification as future work |
| Sandbox webhook delays | Poll status endpoints as a fallback |
| Reminders feel spammy | Escalation caps, quiet hours, per-client opt-out |
