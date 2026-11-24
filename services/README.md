# `services/` — POS Service Layer

Service modules sit between the Flask blueprints (HTTP layer) and the
SQLAlchemy models (data layer). They are **the only place** that should
talk to external systems (Daraja, Flutterwave, Africa's Talking, Redis) or
encode multi-step business rules (loyalty maths, receipt formatting,
auditing).

Blueprints in `api/` should never call `requests.post(...)` directly — they
call into `services/*` and translate domain errors to HTTP responses.

| Module | Talks to | Raises | Safe to call without external deps? |
|---|---|---|---|
| [`daraja`](daraja/)                           | Safaricom Daraja, per business  | `DarajaError`       | No — needs network + the business's creds |
| [`billing`](billing/)                         | Invoices, payments, allocation  | `BillingError`      | Yes (database) |
| [`mpesa_service`](mpesa_service.py)           | Phone format, STK callback parse | `ValueError`       | Yes |
| [`flutterwave_service`](flutterwave_service.py) | Flutterwave v3                  | `FlutterwaveError`  | No — needs network + creds |
| [`sms_service`](sms_service.py)               | Africa's Talking, `sms_messages` | never for provider problems | Yes — records `failed`, returns the row |
| [`stock`](stock.py)                           | Batches, variants, line batches | `StockError`, `InsufficientStock` | Yes (database) |
| [`clock`](clock.py)                           | Nothing                         | never               | Yes |
| [`receipt_service`](receipt_service.py)       | DB (read-only)                  | `ValueError`        | Yes |
| [`loyalty_service`](loyalty_service.py)       | DB (read+write, no commit)      | `LoyaltyError`      | Yes |
| [`audit_service`](audit_service.py)           | DB (write, no commit)           | never (logs)        | Yes |
| [`otp_service`](otp_service.py)               | Redis (with in-memory fallback) | `ValueError`        | Yes |
| [`database_service`](database_service.py)     | MySQL admin                     | logs                | Yes (existing) |
| [`seed_service`](seed_service.py)             | DB                              | logs                | Yes (existing) |

---

## Conventions

1. **Blueprints commit, services flush.** Services that mutate the DB call
   `db.session.add(...)` and `db.session.flush()` so the row gets an ID, but
   the surrounding blueprint owns `db.session.commit()` / `rollback()`. This
   keeps multi-step POS flows atomic (e.g. complete-transaction = update stock
   + award loyalty + audit log + commit once). Exceptions, because they call
   out to a provider and must leave a record even if the request then fails:
   `sms_service.send()` and `billing.stk.start_stk()` commit, so call them
   after your own commit.
2. **External calls never crash a checkout.** SMS failures are recorded as
   `failed` rows and the helpers return `False`. Daraja / Flutterwave failures raise typed errors that the blueprint
   should catch and return a proper HTTP status for.
3. **Money is `Decimal`.** Receipts cast to `float` only at the very edge
   (JSON output / printer). Internal arithmetic uses `Decimal` to avoid
   floating-point drift on prices.
4. **Phone numbers use `services.phone.normalize_ke_phone()`** before any AT or
   Daraja call. Accepts `+254...`, `07...`, `01...`, `7...`, `1...`, `254...`.

---

## `billing/` — invoices and payments (the only writer)

Every sale is an `Invoice`; money received is a `Payment` allocated to
invoices (`PaymentAllocation`); credit notes are `InvoiceAdjustment`; STK
pushes are `PaymentIntent`. See `models/billing.py` for the money model.

```python
from services import billing
inv = billing.create_invoice(total=1500, kind="credit", customer_id=c.id, due_date=d)
billing.record_payment(method="mpesa_c2b", amount=1500, receipt="SJK82910PL",
                       bill_ref=inv.invoice_number)       # idempotent on the receipt
billing.adjust(inv, 200, "return")                         # credit note
billing.void_invoice(inv); billing.reallocate(p, other); billing.reverse_payment(p)
billing.customer_exposure(c.id); billing.review_queue()
```

Runs in the bound tenant; callers commit. `billing.stk` handles STK pushes,
Daraja callbacks, Paybill confirmations and the sweep
(`flask payments-sweep`, every 10 minutes). Regression stories:
`tests/evals/reconciliation_cases.json`.

## `daraja/` — Safaricom Daraja, one business at a time

`DarajaClient(tenant)` uses that business's own shortcode, keys and
environment: `stk_push`, `query` (paid / failed / processing),
`register_c2b` (always `ResponseType: Completed`). Paybill pushes use
`CustomerPayBillOnline`; Till pushes `CustomerBuyGoodsOnline` with the store
number as shortcode and the till as PartyB. Tokens are cached in Redis per
business (`daraja:token:<id>`, TTL `expires_in - 120`), memory fallback.
Callback URLs: `/api/payments/daraja/{stk|c2b}/<slug>/<callback_token>`.

## `stock.py` — the only stock writer

```python
from services import stock
stock.receive(item, 50, batch_number="AMX-7", expiry_date=d, unit_cost=12)
taken = stock.take(item, 10, transaction_item=line)   # FEFO, skips expired; [(batch, n)]
stock.give_back(item, 3, transaction_item=line)        # back to the batches it came from
stock.adjust(item, -4, reason="expired_write_off")     # takes expired stock too
```

Locks item, then batches by id, then variant. Raises `InsufficientStock`
before changing anything. Flushes; callers commit. Items with
`has_variants` need `variant=`.

## `sms_service.py` — the outbox

`send(to, body, kind, dedupe_key=None)` writes and commits a
`sms_messages` row, then calls Africa's Talking with `+254...`. Call it after
your own commit (it raises `UnsavedChanges` otherwise). `record_delivery()`
applies delivery reports, forwards only. `SendSMS.*` wraps the app's
messages and returns True when accepted.

## `clock.py`

`today_nairobi()` / `now_nairobi()`: expiry, aging and daily jobs use these,
never `date.today()` (the server runs on UTC).

## `mpesa_service.py` — helpers

`format_phone()` (Daraja's `2547XXXXXXXX`) and `parse_stk_callback()`.

## `flutterwave_service.py` — Card payments

Thin wrapper around three Flutterwave v3 endpoints we use.

### Required config
- `FLW_SECRET_KEY` — server key (`FLWSECK-...`)
- `FLW_PUBLIC_KEY` — client key (`FLWPUBK-...`)
- `FLW_WEBHOOK_SECRET` — the *Secret hash* configured in the Flutterwave dashboard

### API
```python
from services.flutterwave_service import (
    create_payment_link, verify_transaction,
    validate_webhook_signature, FlutterwaveError,
)
```

| Function | Purpose | Returns |
|---|---|---|
| `create_payment_link(tx_ref, amount, currency, customer_email, customer_name=None, customer_phone=None, redirect_url=None, description=None)` | Create a hosted-checkout URL for the cashier to display / WhatsApp to the customer | `{"payment_link": "...", "raw": <json>}` |
| `verify_transaction(transaction_id)` | Server-side verify by Flutterwave's numeric transaction ID | `{"status", "amount", "currency", "tx_ref", "flw_ref", "raw"}` |
| `validate_webhook_signature(payload, signature)` | Constant-time check of the `verif-hash` header | `bool` |

### Webhook flow
```
+-------------------+   POST /payments/card/webhook
| Flutterwave       |---------------------------+
| (after card pmt)  |  verif-hash: <secret>     |
+-------------------+         body: JSON         v
                                        +-----------------------+
                                        | api/payments.py       |
                                        |  validate_signature   |
                                        |  verify_transaction() |  -> Flutterwave GET /verify
                                        |  mark txn confirmed   |
                                        +-----------------------+
```
**Always re-verify with `verify_transaction()` after a webhook fires.** The
webhook proves *something* happened; the verify call proves *what*.

### Webhook signature
Flutterwave's documented mechanism is a plain shared-secret check: the
dashboard `Secret hash` is sent verbatim in `verif-hash`. We accept that AND
an HMAC-SHA256(body, secret) hex digest as a fallback for callers using the
alternate scheme. Constant-time comparison via `hmac.compare_digest`.

---

## `sms_service.py` — Africa's Talking

Existing `SendSMS.send_order_confirmation` is preserved. Five new triggers
have been added; **all** return `bool` and **never** raise — AT is treated as
best-effort so a network blip cannot block a checkout.

| Method | Trigger |
|---|---|
| `send_order_confirmation(phone, name, details)`     | Existing — order placed |
| `send_payment_confirmation(phone, total, ref)`      | POS transaction completed |
| `send_low_stock_alert(admin_phones, item, stock, reorder)` | Stock ≤ reorder level (nightly job or live) |
| `send_prescription_ready(phone, rx_id)`             | Prescription dispensed |
| `send_credit_reminder(phone, balance)`              | Outstanding credit reminder |
| `send_manager_otp(phone, otp, action_label)`        | Manager-override OTP |

`admin_phones` accepts a single string or a list — empty entries are filtered.

---

## `receipt_service.py` — Receipt formatters

Pure-DB-read service that produces three representations of a completed
transaction:

| Function | Purpose | Output |
|---|---|---|
| `build_receipt(transaction_id)`        | Canonical structured form for JSON / HTML / PDF | `dict` |
| `format_sms_receipt(transaction_id)`   | Short SMS body (~< 160 chars) | `str` |
| `format_thermal_receipt(transaction_id)` | 42-column ESC/POS body (80mm Font A) | `str` |

`build_receipt` is the source of truth — the SMS and thermal formatters call
it. Money values are `float` at the edge for JSON-friendliness.

The thermal format does **not** include cut/feed control codes — your printer
driver should append them. Layout helpers (`_line`, `_center`, `_two_col`)
are private; if you need a different paper width, copy the module rather than
parametrising it (printers vary too much).

Raises `ValueError` for an unknown `transaction_id`.

---

## `loyalty_service.py` — Loyalty points

Integer points, append-only ledger via `LoyaltyTransaction`.

### Rules
- 1 point per `LOYALTY_POINTS_RATIO` KES of net spend (default 10).
- 1 point = 1 KES discount on redemption (caller applies the discount).

### API
```python
from services.loyalty_service import (
    award_points, redeem_points, get_balance, LoyaltyError,
)
```

| Function | Behaviour |
|---|---|
| `get_balance(customer_id)` | Returns `int`. Raises `LoyaltyError` if customer missing. |
| `award_points(customer_id, transaction_id, total_amount)` | Computes points, increments `Customer.loyalty_points`, writes ledger row, **flushes (no commit)**. |
| `redeem_points(customer_id, points, transaction_id)` | Validates balance, decrements, writes ledger row, flushes. Raises on insufficient balance. |

### Composability
`award_points` is designed to be called from inside the POS checkout
transaction so an SMS-send failure or stock-deduction failure can roll the
points back atomically:

```python
try:
    loyalty_service.award_points(customer.id, txn.id, txn.total)
    # ... other steps ...
    db.session.commit()
except Exception:
    db.session.rollback()
    raise
```

---

## `audit_service.py` — Audit log

Single function: `log(user_id, action, entity_type=None, entity_id=None, old_value=None, new_value=None, ip_address=None)`.

Writes to the `audit_logs` table. Behavioural notes:

- Auto-captures `request.remote_addr` (incl. `X-Forwarded-For`) when called
  inside a Flask request context.
- Sanitises `old_value` / `new_value` so a stray `Decimal` / `datetime` won't
  break the JSON column.
- **Never re-raises.** A failing audit write logs an error and returns
  normally — the calling action must not be aborted because auditing failed.
- Flushes but does not commit.

### When to call it
Every destructive or sensitive action: void, refund, manual stock adjustment,
role change, credit-payment posting, prescription edit, supplier delete, PO
cancellation.

```python
from services import audit_service

audit_service.log(
    user_id=session["user_id"],
    action="transaction.void",
    entity_type="Transaction",
    entity_id=txn.id,
    old_value=txn.to_dict(),
    new_value={"status": "voided", "reason": reason},
)
```

---

## `otp_service.py` — Manager-override OTPs

6-digit numeric OTPs with TTL, used to authorise actions a cashier can't
perform alone (post-completion void, large discount, refund).

### Backend selection
- Tries `redis://...` from `Config.REDIS_URL` on first call.
- On any failure (no server, wrong URL, network error) it falls back to a
  process-local dict and **logs a warning once** so operators know not to
  rely on it across multiple gunicorn workers.

### API
```python
from services.otp_service import generate_otp, verify_otp, peek_otp
```

| Function | Behaviour |
|---|---|
| `generate_otp(scope, length=6)` | Returns the OTP string and stores it under `make-an-order:otp:{scope}` for `MANAGER_OTP_TTL` seconds (default 300). |
| `verify_otp(scope, candidate, *, consume=True)` | Constant-time compare. On match, deletes the entry (single-use). Returns `bool`. |
| `peek_otp(scope)` | Debug-only read without consuming. Don't expose via HTTP. |

### Override flow
```
cashier  -> POST /pos/transaction/<id>/void  (first call, no OTP)
api/pos.py
   |
   |-- otp = otp_service.generate_otp(f"override:txn:{id}")
   |-- SendSMS.send_manager_otp(manager_phone, otp, "void")
   |-- return 202 { "otp_required": true }
   v
cashier  -> POST /pos/transaction/<id>/void  (second call, body includes "otp": "123456")
api/pos.py
   |-- if not otp_service.verify_otp(f"override:txn:{id}", body["otp"]):
   |       return 403
   |-- proceed with void
   |-- audit_service.log(...)
   v
```

### Production note
For multi-worker deployments **Redis is required** — the in-memory store is
per-process, so an OTP generated by worker A won't be visible to worker B.

---

## Testing strategy (relevant to Phase 5)

All external HTTP calls go through one of these symbols:

- `services.daraja.client.requests.get` / `.post` (see `tests/daraja_fakes.py`)
- `services.daraja.tokens._get_redis` (return `None` for the memory cache)
- `services.flutterwave_service.requests.get` / `.post`
- `services.sms_service._client` (returns the Africa's Talking SMS client, or `None`
  when unconfigured; patch it to return a fake with a `send` method)

Patch those with `pytest-mock` in tests so the suite never hits the network
and never needs real credentials.

For Redis-touching tests, prefer patching `services.otp_service._get_redis`
to return `None` so the in-memory fallback path is exercised
deterministically.
