# Step 3: Billing and M-Pesa Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every sale is an invoice; every shilling received is a confirmed payment allocated to invoices; each business's M-Pesa settles to its own Till or Paybill and reconciles itself, including partial, split, duplicate and missed callbacks.

**Architecture:** A billing service (`services/billing/`) owns invoices, payments and allocations; nothing else writes them. A per-tenant Daraja client (`services/daraja/`) talks to Safaricom with the tenant's own credentials and a Redis token cache. Public webhooks live in `api/daraja.py` under `/api/payments/daraja/...`, bound to a tenant by slug and callback token. POS checkout, void, refund and the customer credit endpoints are rewired onto the billing service; `customers.credit_balance` is dropped.

**Tech Stack:** as step 2. No new libraries (HTTP via `requests`, already used; Redis via `redis`, already used).

**Spec:** `docs/superpowers/specs/2026-11-24-vertical-saas-pivot-design.md` (Payments, Reversals and corrections, Concurrency, Schema: invoices/payment_intents/payments/payment_allocations/invoice_adjustments). Step 2 plan for conventions.

## Global Constraints

- Money is `Numeric(12,2)`, handled as `Decimal`, never float, inside the billing service.
- `invoice_number` is unique per tenant, `INV` + 6 digits (fits Daraja's 12-char AccountReference).
- `balance = total - amount_adjusted - amount_paid`; `amount_paid` and `amount_adjusted` are recomputed from live rows while the invoice row is locked (`with_for_update`).
- `(tenant_id, mpesa_receipt)` unique; receipts stored stripped and upper-cased; empty -> NULL.
- STK amount = balance rounded **up** to the shilling; cents over stay unallocated.
- Webhook paths never contain "mpesa" or "safaricom". Responses, always HTTP 200 JSON:
  STK `{"ResultCode": 0, "ResultDesc": "Accepted"}`; C2B confirmation `{"ResultCode": 0, "ResultDesc": "Confirmation received successfully"}`; C2B validation `{"ResultCode": 0, "ResultDesc": "Accepted"}`. Wrong slug or token: 404, nothing recorded.
- C2B registration always `"ResponseType": "Completed"`.
- No bulk writes (step 2 rule): change rows by locked load + edit.
- Every test runs on SQLite and MySQL 5.7 (CI).

## Decisions made in this plan (Mark can overturn)

- **POS M-Pesa no longer completes on a cashier's click.** "Mark received" now checks that the invoice is actually paid (STK callback, Paybill confirmation, or a typed M-Pesa code). A sale cannot be completed as M-Pesa without money recorded against it.
- **A physical card machine (PDQ) payment stays allowed at the counter** as method `card` with its reference; that money goes to the business's bank. Flutterwave online cards stay off (step 2).
- **Credit term:** `customers.credit_term_days` (default 30) is added now, since a credit invoice needs a due date; the distributor step adds the rest of the duka fields.
- **Payments record their payer** (`payments.customer_id`, nullable) so unallocated money shows as that customer's credit.
- **`tenants.manual_mpesa_enabled`** (default on) controls typed M-Pesa codes; owners switch it off once Daraja is live.
- Old global `/payments/mpesa/callback` is removed; pushes now carry the per-tenant URL.

## Review Focus

1. The same M-Pesa receipt arriving through STK callback, C2B confirmation and a typed code in any order, including concurrently: one payment, allocated once. Tests in Task 2 and Task 4.
2. A Daraja callback that never arrives: the sweep finds the money by STK query; a query that keeps saying "processing" does not close the intent early. Task 4.
3. Money larger, smaller or later than the invoice: overpay rolls to the payer's next invoice, underpay leaves it partial, a payment after the invoice was voided or expired is not lost. Task 2.
4. Credit-limit races: two credit sales for one customer at once cannot both pass the ceiling. Task 5 (MySQL lane).
5. A webhook for business A posted to business B's URL, or with a wrong token: nothing recorded. Task 4.

---

### Task 1: Billing schema (migration 0004)

**Files:** create `models/billing.py`, `migrations/versions/0004_billing.py`; modify `models/__init__.py`, `models/core.py` (drop `credit_balance`, add `credit_term_days`), `models/base.py` (`manual_mpesa_enabled`); test `tests/test_migrations_billing.py`.

**Interfaces (produces):**
- `Invoice(TenantMixin)`: `invoice_number`, `customer_id?`, `transaction_id?`, `order_id?`, `kind` (`sale`|`credit`|`opening_balance`), `total`, `amount_paid`, `amount_adjusted`, `status` (`open|partial|paid|void|expired`), `issued_at`, `due_date?`, `pay_token`, `etims_status` (default `not_submitted`), `etims_cuin?`, `etims_qr_url?`, `external_etims_ref?`, `notes?`. Property `balance`. Unique `(tenant_id, invoice_number)`, `(tenant_id, pay_token)`.
- `PaymentIntent(TenantMixin)`: spec columns (`checkout_request_id?` unique per tenant, `merchant_request_id?`, `invoice_id`, `phone`, `amount`, `status` `initiating|pending|callback_received|completed|failed|timeout`, `result_desc?`, `raw_callback?` JSON, `payment_id?`, `last_queried_at?`, `created_at`).
- `Payment(TenantMixin)`: spec columns plus `customer_id?`; `method` `mpesa_stk|mpesa_c2b|mpesa_manual|cash|card`; `status` `confirmed|reversed`; `match_status` `matched|partial|unmatched`; `@validates('mpesa_receipt')` strip/upper/empty->None; unique `(tenant_id, mpesa_receipt)`.
- `PaymentAllocation(TenantMixin)`: `payment_id`, `invoice_id`, `amount`, `voided_at?`, `voided_by?`, `created_at`.
- `InvoiceAdjustment(TenantMixin)`: `invoice_id`, `amount`, `reason` (`return|rejected_delivery|void|correction`), `note?`, `created_by?`, `created_at`.
- `0004_billing`: creates the five tables; adds `customers.credit_term_days` (default 30), `tenants.manual_mpesa_enabled` (default true); every customer with `credit_balance > 0` gets an `opening_balance` invoice (numbered per tenant, due today, open); drops `credit_balance`. Downgrade restores `credit_balance` from open invoice balances, then drops the tables (refuses if any non-opening invoice has payments, since that history cannot be put back into one number).

- [ ] Tests: opening balances become invoices with the right totals and numbers per tenant; zero balances create none; head matches models; legacy upgrade to head still zero-drift; downgrade restores balances.
- [ ] Implement, run on SQLite and local MySQL, commit `feat: billing schema (invoices, payments, allocations)`.

### Task 2: Billing service

**Files:** create `services/billing/__init__.py`, `invoices.py`, `payments.py`, `matching.py`; tests `tests/test_billing.py`, `tests/evals/reconciliation_cases.json`, `tests/test_reconciliation_cases.py`.

**Interfaces (produces), all running in the bound tenant:**
- `create_invoice(*, total, kind, customer_id=None, transaction_id=None, order_id=None, due_date=None, notes=None) -> Invoice` (numbers with retry on a unique collision).
- `recompute(invoice) -> Invoice` (locks; sets `amount_paid`, `amount_adjusted`, `status`; an `expired` invoice that receives money reopens).
- `record_payment(*, method, amount, receipt=None, intent=None, invoice=None, bill_ref=None, payer_phone=None, payer_name=None, customer_id=None, org_account_balance=None, raw=None, recorded_by=None, verified=True) -> Payment`: idempotent on receipt (insert under a savepoint; on duplicate, load with lock and merge: fill missing fields, flip a manual entry to verified, or on an amount mismatch keep Daraja's amount, move the typed one to `claimed_amount`, redo allocations and write an `AuditLog`); then allocates.
- `allocate(payment, target=None)`: target first (up to its balance), then the payer's other open/partial invoices oldest first (skipping expired/void unless targeted), remainder stays `unallocated_amount`; sets `match_status`.
- `match_bill_ref(bill_ref) -> Invoice | None` and `C2B_MATCHERS` (list of callables; later steps append member and account matchers).
- `adjust(invoice, amount, reason, note=None, user_id=None) -> InvoiceAdjustment`: credit note; if it leaves `amount_paid` above what is owed, voids allocations newest first by the excess and re-allocates that money to the payer's next invoices.
- `void_invoice(invoice, user_id)`: status void, all its allocations voided, money re-allocated or left as credit.
- `reallocate(payment, to_invoice, user_id)`, `reverse_payment(payment, user_id)`.
- `customer_exposure(customer_id) -> Decimal`: open+partial credit/opening invoices' balances minus the customer's unallocated confirmed payments.
- `review_queue() -> dict`: unmatched payments, unverified manual payments, amount mismatches (`claimed_amount != amount`), possible missed Paybill payments (consecutive `org_account_balance` rising by more than the payment).

- [ ] Tests: exact pay; partial then remainder; overpay rolls to next invoice; one payment across three invoices; unmatched -> queue; duplicate receipt (any order of STK/C2B/manual) -> one payment one allocation; manual 5,000 vs Daraja 500 -> 500, invoice reopened, claimed kept, audit row; credit note after full payment -> balance 0, 200 credit, rolls to next invoice; void a paid invoice -> money becomes credit; reallocate between invoices; reverse -> invoices reopen; expired invoice paid late reopens and is paid; exposure; review queue; gap hint.
- [ ] `tests/evals/reconciliation_cases.json`: list of scenarios (invoices, payments in order, expected invoice statuses/balances and unallocated amounts); `test_reconciliation_cases.py` runs each.
- [ ] Commit `feat: billing service (invoices, payments, allocation, credit notes)`.

### Task 3: Per-tenant Daraja client

**Files:** create `services/daraja/__init__.py`, `client.py`, `tokens.py`; modify `services/mpesa_service.py` (keep only `format_phone`, `parse_stk_callback`; everything else moves); tests `tests/test_daraja_client.py`.

**Interfaces:**
- `DarajaClient(tenant)`: `stk_push(phone, amount, account_ref, description) -> dict`, `query(checkout_request_id) -> QueryResult(state='paid'|'failed'|'processing', result_code, result_desc)`, `register_c2b() -> dict`. Paybill: `CustomerPayBillOnline`, PartyB = shortcode. Till: `CustomerBuyGoodsOnline`, BusinessShortCode = store number (`daraja_shortcode`), PartyB = `daraja_till_number`. Callback URL `{APP_URL}/api/payments/daraja/stk/{slug}/{callback_token}`.
- Errors: `DarajaNotConfigured`, `DarajaBusy` (error code `500.001.1001` on push), `DarajaError`.
- `tokens.get_token(tenant) -> str`: Redis key `daraja:token:{tenant_id}`, TTL `int(expires_in) - 120`; in-process dict fallback when Redis is unreachable (the `otp_service` pattern).
- [ ] Tests (requests stubbed): paybill and till payloads; amount ceil (100.50 -> 101); `500.001.1001` on push -> `DarajaBusy`; query `500.001.1001` -> processing; query 1032 -> failed; 0 -> paid; token cached once per tenant, separate per tenant, Redis down falls back; no URL contains "mpesa"/"safaricom"; register payload has `"ResponseType": "Completed"` and both URLs.
- [ ] Commit `feat: per-tenant Daraja client with token cache`.

### Task 4: Webhooks, STK flow, sweep

**Files:** create `api/daraja.py` (public), `services/billing/stk.py`; modify `api/payments.py` (staff STK push, intent status, manual entry, review queue, assign/reverse; remove old callback), `app.py` (register blueprint, `flask payments sweep`); tests `tests/test_daraja_webhooks.py`, `tests/test_stk_flow.py`.

**Interfaces:**
- `start_stk(invoice, phone, user_id) -> PaymentIntent`: refuses (`PromptPending`) if the phone has an intent `initiating|pending` younger than 90 s; writes the intent `initiating` and commits before calling Daraja; on success `pending` with ids; on `DarajaBusy` -> intent `failed` with a "wait a minute" message, invoice untouched.
- `handle_stk_callback(payload)`: saves `raw_callback`; non-zero result -> `failed`; success -> verifies with `query` (5 s timeout): paid -> `record_payment(method='mpesa_stk', intent=...)`, `completed`; query unavailable or processing -> `callback_received` (sweep finishes it).
- `handle_c2b(payload)`: `record_payment(method='mpesa_c2b', receipt=TransID, amount=TransAmount, bill_ref=BillRefNumber, payer_name, payer_phone=MSISDN, org_account_balance)`; allocation by `match_bill_ref`, then `C2B_MATCHERS`.
- `sweep(now) -> dict`: per active tenant, intents `pending|callback_received` older than 90 s are queried (`processing` keeps them, updates `last_queried_at`; `paid` records from the saved callback or, with none, marks `completed` once the receipt arrives via C2B; `failed` closes); older than 10 min with no terminal answer -> `timeout`.
- Routes: `POST /api/payments/daraja/stk/<slug>/<token>`, `POST /api/payments/daraja/c2b/<slug>/<token>`, `POST .../validate`; optional `DARAJA_ALLOWED_IPS`; token compared with `hmac.compare_digest`; token redacted in logs.
- Staff: `POST /payments/mpesa/stk_push {transaction_id|invoice_id, phone_number}`, `GET /payments/intents/<id>`, `POST /payments/manual {invoice_id, receipt, amount}` (cashier+, needs `manual_mpesa_enabled`), `GET /payments/review` (admin), `POST /payments/<id>/assign {invoice_id}` and `POST /payments/<id>/reverse` (owner).
- [ ] Tests: exact bodies byte for byte; wrong token / other tenant's slug -> 404, nothing written; STK success -> payment + allocation; STK cancelled -> intent failed; query outage -> callback_received then sweep completes; duplicate callback once; C2B then STK and STK then C2B same receipt -> one payment; till C2B first then STK -> matched; manual then webhook; one prompt per phone in 90 s; busy push; sweep "processing" keeps pending, 10 min -> timeout; Paybill gap hint.
- [ ] Commit `feat: per-tenant Daraja webhooks, STK intents and sweep`.

### Task 5: POS, credit and refunds on billing

**Files:** modify `api/pos.py` (complete, void, refund), `api/customers.py` (credit summary and payment), `templates/pos.html` (poll the intent; "Mark received" only completes when paid), `services/receipt_service.py` (invoice number on receipts); tests `tests/test_pos_billing.py`.

**Behaviour:**
- `complete`: creates the transaction's invoice if it has none. `cash`/`card` -> payment + allocation; `credit` -> lock customer row, refuse if `customer_exposure + total > credit_limit`, invoice `credit` due `today + credit_term_days`; `mpesa` -> refuse with 409 unless the invoice is paid; `split` -> cash and card parts recorded, M-Pesa part must already be paid onto the invoice.
- `void` of a completed sale -> `void_invoice`; money already paid becomes customer credit (or unallocated for walk-ins) and shows in the review queue for a cash refund.
- `refund` -> `adjust(invoice, refunded_value, 'return')`.
- `GET /customers/<id>/credit` -> limit, exposure, available, open invoices; `POST /customers/<id>/credit/payment` -> `record_payment(method=cash|card|mpesa_manual, customer_id=...)` allocated oldest first.
- [ ] Tests: each method end to end with invoice state; M-Pesa completion refused until paid, allowed after callback; credit ceiling; concurrent credit sales (MySQL lane, two threads); void paid -> credit; partial refund -> credit note; credit payment across invoices.
- [ ] Commit `feat: POS checkout, credit and refunds on invoices`.

### Task 6: Tenant settings and CLI

**Files:** create `api/settings.py`, `templates/settings.html`; modify `app.py` (CLI group `tenants`); tests `tests/test_settings.py`.

- Settings (owner): Daraja shortcode, type, till number, env, consumer key, secret, passkey (write-only: shown as "set"/"not set"; blank keeps the stored value), sender id, KRA PIN, PPB licence and expiry, superintendent reg no, manual M-Pesa on/off, "rotate callback token" (re-registers C2B when configured), and the two callback URLs to give Safaricom.
- `flask tenants import-env-credentials <slug>` (old `MPESA_*` env into a tenant), `flask tenants register-c2b <slug>`.
- [ ] Tests: secrets never in any response; blank keeps; rotation changes URL and old token 404s; non-owner 302/403; CLI import and register (Daraja stubbed).
- [ ] Commit `feat: tenant settings page and Daraja CLI`.

### Task 7: Sweep, docs, review

- [ ] Isolation sweep picks up the new routes automatically; add webhook routes with A's slug/token posted by an anonymous client carrying B's references.
- [ ] README (payments section, settings, CLI, env), `.env.example`.
- [ ] Full suite SQLite + local MySQL; CI green; Codex review (gpt-6.1-sol, medium, neutral wording); fix; stop for review.
