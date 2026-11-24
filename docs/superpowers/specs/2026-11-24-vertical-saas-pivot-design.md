# make-an-order: vertical SaaS pivot (design, schema, migration plan)

## Context

`make-an-order` is a single-business Flask POS. Mark is pivoting it into a multi-tenant SaaS for Kenyan businesses with three vertical faces (pharmacy, distributor, institution) on one shared core, to pilot in Nairobi at KES 3,500-5,000/month. Merchants will not adopt it unless M-Pesa money lands in their own Till/Paybill and reconciles without typing codes.

What the code is today, and why this is more than "add tenant_id":

- 19 tables, 128 query sites in 13 files, none tenant-aware. Business type is one global `SystemConfig` row.
- No Alembic migrations. Schema is `db.create_all()` plus hand-written ALTERs in `app.py:69-127`.
- `Product`, `Batch`, `Payment`, `Invoice`, `ControlledDrugLedger` do not exist. Product is `InventoryItem`; batch and expiry are two columns on it (`models.py:119-120`); the controlled-drug "ledger" is computed from sales with no running balance (`api/pharmacy.py:216`).
- M-Pesa is single-merchant: global credentials and one token cache (`services/mpesa_service.py:33`), Paybill hard-coded (`:142`), and the callback marks the whole sale paid whatever amount arrived (`api/payments.py:48`).
- Tests import `app`, which connects to MySQL at import time (`app.py:62`).

Decisions Mark made: cover all five parts (core, three verticals, scheduler); enforce isolation with an automatic fail-closed tenant filter.

## Goals, non-goals, anti-goals

Goals
- One database, every row owned by a tenant, cross-tenant reads and writes impossible by construction.
- Per-tenant Daraja credentials; STK and C2B Paybill money settles to the merchant and reconciles to invoices, including partial and split payments.
- Zero-login public links at `/o/<tenant_slug>` per vertical.
- Vertical dashboard replaces the POS as the landing page.
- Daily SMS triggers: Day-26 refill reminders and overdue credit reminders.

Non-goals (this build)
- Live KRA eTIMS submission. We store `kra_pin`, the encrypted token, and per-invoice eTIMS status/CUIN/QR fields, and show the badge. A submit adapter interface exists with a "not submitted" default. Real OSCU/VSCU integration is its own project (needs KRA sandbox onboarding).
- Web upload UI for CSV, WhatsApp share buttons, PPB quarterly returns PDF, duka statement PDF (Phase 3 of the business plan). The PPB inspection PDF and a CLI CSV import are in scope.
- Daraja Pull Transactions and Transaction Status APIs (extra Safaricom approvals). Missed Paybill callbacks are covered by the balance hint and manual code entry below.
- Subscription billing. `subscription_status` and `trial_ends_at` gate access; collecting the SaaS fee is manual.
- Card payments. Flutterwave uses platform keys, so money would sit with us. It is switched off for tenants, code left in place.

Anti-goals
- Any path where one merchant sees another's data.
- Any path where customer money touches a platform account.
- A payment silently lost: every shilling received is a `Payment` row, matched or queued for review.

## Approaches considered

1. Automatic tenant filter, fail-closed. Chosen. One mixin plus session hooks scope all 128 query sites; an unbound query raises.
2. Explicit `tenant_id` filter at every call site. Rejected: one missed query leaks patient or debtor data.
3. New app beside the POS. Rejected: pharmacies need counter sales, so the POS would need tenanting later with two codebases.
4. Database per tenant. Rejected: the brief specifies `tenant_id` keys, and per-merchant migrations are heavy for a pilot.

## Design

### Tenancy (`auth/tenancy.py`, `models/base.py`)

- `TenantMixin` adds `tenant_id` (FK `tenants.id`, NOT NULL, indexed). Every table carries it except `tenants`. Child rows (`order_items`, `transaction_items`, and so on) carry it too, so the rule has no exceptions.
- Session hooks:
  - `do_orm_execute`, selects: adds `with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tid, include_aliases=True)`, which also covers lazy loads. `tid` is a local read from the bound tenant on each execution, so the cached statement takes it as a bound parameter and never bakes in one tenant's id.
  - `do_orm_execute`, bulk `update()` and `delete()`: the hook does not rely on loader criteria here. It appends `.where(Model.tenant_id == tid)` itself for any `TenantMixin` target.
  - No tenant bound raises `TenantNotBound` for selects, updates and deletes alike.
  - Binding, switching or clearing the tenant calls `db.session.expunge_all()`, so `session.get()` can never return another tenant's object from the identity map (the scheduler loop and tests switch tenants in one process).
  - `before_flush`: stamps `tenant_id` on new rows; raises if a row's tenant differs from the bound one.
  - `AuditLog` is the one exception to NOT NULL: it overrides `tenant_id` as nullable, and `before_flush` allows a null tenant on an `AuditLog` row only inside `unscoped()` (platform events).
  - Anything the hook cannot scope is refused outside `unscoped()`: non-ORM statements passed to `session.execute()` (`is_orm_statement` false), and ORM `insert()` statements on tenant models (they skip `before_flush`). Rows are created with `session.add()` only.
  - `unscoped()` context manager for the few platform paths: login lookup, slug resolution, scheduler tenant loop, Africa's Talking `/delivery-reports`, password reset and OTP lookup. Each use is listed in a test allowlist. Leaving `unscoped()` also calls `expunge_all()`.
  - Listeners are registered once at module import, not per `create_app()` call, so building two apps in one process does not double-fire them.
- `@tenant_required`: reads `session['tenant_id']`, loads the tenant, confirms on every request that the logged-in user still belongs to it and is approved, checks subscription (expired trial renders a "trial ended" page, API returns 402), sets `g.current_tenant`. The existing role decorators in `auth/auth_middleware.py` are rebuilt on top of it so no route can skip it.
- `@public_tenant`: binds from `<tenant_slug>` for `/o/...` and webhooks. Unknown slug is 404.
- Users: `email` stays globally unique (login has to find the tenant from the email alone). `(tenant_id, phone_number)` unique. Known limit: one email works for one tenant, so a locum at two chemists needs two emails. `users.tenant_id` is nullable only when `is_platform_admin` is true, the second and last exception alongside `AuditLog`; a platform admin picks a tenant to act in. `users.ppb_reg_no` added for pharmacists. Platform admins have `is_platform_admin=True`, set from env `PLATFORM_ADMIN_EMAILS`; the hard-coded `SUPERUSER_EMAIL` in `models.py:6` goes.
- Signup creates a `Tenant` (name, slug, vertical, 14-day trial) plus its first admin. `templates/onboarding.html` becomes the vertical picker. `BUSINESS_CONFIGS` in `app.py:174` shrinks to three verticals.
- Tenant Settings screen (admin only, built with step 3): Daraja shortcode, type and keys, sender id, KRA PIN, PPB licence details, manual-entry on/off, token rotation. Secrets are write-only: the form shows "set" or "not set" and never echoes a stored value.
- Secrets: `EncryptedString` column type using Fernet (`cryptography` is already installed), key from env `TENANT_SECRETS_KEY`. Never returned by `to_dict()`.

### Schema

New and changed tables. Money is `Numeric(12,2)`. All have `id`, `tenant_id`, `created_at` unless noted.

`tenants` (new, no tenant_id)
- `name`, `slug` (unique), `vertical` enum(pharmacy, distributor, institution)
- `kra_pin`, `etims_token_encrypted`
- `daraja_shortcode`, `daraja_shortcode_type` enum(till, paybill), `daraja_till_number` (nullable; Buy Goods STK needs store number as shortcode and till number as PartyB), `daraja_consumer_key_encrypted`, `daraja_consumer_secret_encrypted`, `daraja_passkey_encrypted`, `daraja_env` (sandbox/production), `callback_token` (random, see Payments)
- `at_sender_id`, `subscription_status` (trial/active/past_due/cancelled), `trial_ends_at`
- `ppb_premises_license`, `ppb_premises_license_expiry`, `superintendent_reg_no` (pharmacy only, nullable)

Existing tables: add `tenant_id`, swap global uniques for per-tenant ones

| Table | Old unique | New unique |
|---|---|---|
| `users` | email | email (kept global), (tenant_id, phone_number) |
| `categories` | name | (tenant_id, name) |
| `inventory_items` | sku; barcode | (tenant_id, sku); (tenant_id, barcode) |
| `customers` | phone_number; code | (tenant_id, phone_number); (tenant_id, code); (tenant_id, account_ref) |
| `transactions` | transaction_ref | (tenant_id, transaction_ref) |
| `purchase_orders` | po_number | (tenant_id, po_number) |
| `system_config` | key | table dropped; values move to `tenants` |

`suppliers`, `orders`, `order_items`, `stock_movements`, `shifts`, `cash_movements`, `transaction_items`, `purchase_order_items`, `prescriptions`, `prescription_items`, `loyalty_transactions`, `audit_logs` get `tenant_id` only (`audit_logs.tenant_id` nullable for platform events).

`inventory_items` (this is "Product"; not renamed, 50+ references)
- add `drug_schedule` (nullable string), `is_controlled` bool, `is_returnable_container` bool, `has_variants` bool default false
- drop `batch_number`, `expiry_date` (moved to `batches`); `stock` stays as the running total

`batches` (new): `inventory_item_id`, `batch_number`, `expiry_date`, `quantity_on_hand`, `unit_cost`, `received_at`. Unique (tenant_id, inventory_item_id, batch_number). Sales pick the earliest expiry first (FEFO), skipping batches whose `expiry_date` is before today in Nairobi; expired stock leaves only through an adjustment or destruction entry.

`line_batches` (new): `transaction_item_id` nullable, `order_item_id` nullable, `batch_id`, `quantity`. One sale line can draw from several batches (4 from batch A, 6 from batch B), so there is no single `batch_id` on the line. Every reservation, release, void and return reads these rows to put stock back in the exact batch it came from. A controlled sale across two batches writes two ledger rows.

`item_variants` (new): `inventory_item_id`, `label` (for example "Size 32"), `price_override` nullable, `stock`. Unique (tenant_id, inventory_item_id, label).

Stock source of truth: an item has variants or batches, never both, set by `inventory_items.has_variants`.
- With variants: stock lives on `item_variants.stock`; `inventory_items.stock` is the sum of its variants.
- Without: stock lives on `batches.quantity_on_hand`; `inventory_items.stock` is the sum of its batches.
- `inventory_items.stock` is never written directly. One function, `services/stock.py: adjust_stock()`, is the only writer; it locks, changes the variant or batch, updates the total and writes the `StockMovement`. Existing direct writes in `api/pos.py` and `api/inventory.py` move to it.

`customers`
- add `account_ref` (duka code used as Paybill account), `public_token` (random, for the duka order link), `national_id`, `physical_address`, `kra_pin`, `route_zone`, `gps_lat`, `gps_lng`, `credit_term_days` (7/14/30), `price_tier_id`
- drop `credit_balance`. Balance becomes the sum of open invoice balances, so it cannot drift. `credit_limit` stays as the ceiling.

`invoices` (new): `invoice_number` (unique per tenant, max 12 chars so it fits Daraja's AccountReference, for example `INV000123`), `customer_id` nullable, `member_id` nullable, `order_id` nullable, `transaction_id` nullable, `total`, `amount_paid`, `status` (open/partial/paid/void/expired), `issued_at`, `due_date` (set only on credit invoices; NULL on pay-now invoices), `pay_token` (random, for the SMS pay link), `etims_status` (not_submitted/pending/accepted/failed), `etims_cuin`, `etims_qr_url`, `external_etims_ref` (a CUIN the merchant pastes in after raising the invoice on KRA's own portal). One formula everywhere: `balance = total - adjustments - amount_paid`, where `amount_paid` is the sum of live allocations, recomputed while holding a lock on the invoice row.

`payment_intents` (new, one row per STK push; a request, not money): `checkout_request_id` (nullable until Daraja returns it; unique per tenant), `merchant_request_id`, `invoice_id`, `phone`, `amount`, `status` (initiating/pending/callback_received/completed/failed/timeout), `raw_callback` JSON, `payment_id` nullable, `last_queried_at`. The row is written as `initiating` before the push and updated when Daraja answers, so a crash mid-call leaves a trace.

`payments` (new, confirmed money only; one row per M-Pesa receipt): `method` (mpesa_stk/mpesa_c2b/mpesa_manual/cash), `amount`, `unallocated_amount`, `status` (confirmed/reversed), `match_status` (matched/partial/unmatched), `verified` bool (false for a hand-typed code until Daraja confirms it), `claimed_amount` nullable (what staff typed, kept when Daraja disagrees), `mpesa_receipt` (nullable; stored stripped and upper-cased by a `@validates` hook, so `sjk82910pl` and `SJK82910PL` collide, and an empty string becomes NULL so cash payments never clash on the unique key), `bill_ref_raw`, `payer_phone`, `payer_name`, `org_account_balance` Numeric(14,2) nullable, `recorded_by` nullable (staff user for manual and cash), `raw_payload` JSON, `received_at`. Unique (tenant_id, mpesa_receipt) is the idempotency key across STK callbacks, C2B confirmations, manual entry and Daraja retries.

`payment_allocations` (new): `payment_id`, `invoice_id`, `amount`, `voided_at` nullable, `voided_by` nullable. Reallocation voids a row and writes a new one, so the history stays.

`invoice_adjustments` (new, credit notes): `invoice_id`, `amount`, `reason` (return/rejected_delivery/void/correction), `note`, `created_by`. Invoice `balance` is `total - adjustments - amount_paid`. Needed because the POS refund path (`api/pos.py:707`) used to edit `credit_balance`, which is gone.

`orders` (the legacy table, repurposed as the remote order from a public link or staff)
- add `order_number` (unique per tenant), `channel` (public_link/staff), `fulfillment_status`, `delivery_address`, `prescription_id`, `campaign_id`, `member_id`, `reserved_until` nullable, `pickup_code_hash`, `picked_up_at`, `picked_up_by`
- `order_items` add `variant_id`
- `Transaction` stays as the walk-in counter sale. Every `Transaction` and every `Order` gets an `Invoice`, including cash sales paid on the spot (invoice, cash payment and allocation written together). One accounting path, and M-Pesa at the counter always has an invoice number to use as the account reference.

`prescriptions`
- add `prescriber_reg_no`, `is_chronic`, `duration_days` (14/30/90), `last_dispensed_at`, `refill_due_date`, `refills_remaining`, `refill_status` (active/paused/ended), `refill_token`. `doctor_name` is the prescriber name.

`controlled_drug_ledger` (new, append-only; ORM raises on update or delete)
- `inventory_item_id`, `batch_id`, `batch_number` (snapshot), `drug_schedule`, `entry_type` (receipt/dispense/adjustment/destruction), `quantity_in`, `quantity_out`, `opening_balance`, `closing_balance`, `seq` (unique per tenant and item, so balances chain), `prescription_id`, `prescriber_name`, `prescriber_reg_no`, `patient_name`, `patient_national_id`, `patient_address`, `pharmacist_id`, `transaction_id`, `entry_date`
- add `pharmacist_name`, `pharmacist_reg_no` (snapshots from `users`, so the register survives staff leaving)
- add `supplier_name`, `reference_doc_no` (supplier invoice or delivery note, required on receipt), `witness_name`, `witness_reg_no` (required on destruction and adjustment), `notes`, `reverses_entry_id` nullable
- Patient and prescriber details are snapshots, so later edits to a customer do not rewrite the register.
- The exact column set is checked against a real PPB register with a pilot pharmacist before the PDF layout is fixed.

`price_tiers` (new): `name`. `tier_prices` (new): `price_tier_id`, `inventory_item_id`, `price`; unique per tier and item.

`customer_phones` (new, alternate payers): `customer_id`, `phone`, `label`. Unique (tenant_id, phone).

`container_movements` (new): `customer_id`, `inventory_item_id`, `delta` (+issued, -returned), `order_id` nullable, `note`. Balance per duka is the sum.

`members` (new, students or houses): `member_ref` (admission or house number, unique per tenant), `name`, `group_label` (class/stream or court), `guardian_phone`, `secondary_phone` nullable (receipts and pickup codes go to both). Separate from `customers` because one guardian phone can have several children, which the (tenant_id, phone) rule forbids on `customers`.

`campaigns` (new): `name`, `slug` (unique per tenant), `description`, `id_label` ("Admission No" / "House No"), `opens_at`, `closes_at`, `status`, `allow_installments`, `min_installment`. `campaign_items` (new): `campaign_id`, `inventory_item_id`, `price`, `requires_variant`.

`sms_messages` (new): `to_phone`, `body`, `kind`, `dedupe_key`, `status`, `provider_message_id`, `delivery_status`, `sent_at`. Unique (tenant_id, dedupe_key), so a rerun of the daily job cannot send twice.

Phone numbers: every phone column (`users`, `customers`, `customer_phones`, `members`, `payment_intents`, `sms_messages`) stores `254XXXXXXXXX`, enforced by a SQLAlchemy `@validates` hook. The logic already exists as `format_phone` in `services/mpesa_service.py:48` (handles `07`, `01`, `+254`, `254`); it moves to `services/phone.py` as `normalize_ke_phone` and `mpesa_service` imports it.

### Migration plan

Flask-Migrate is installed but unused. `db.create_all()`, `migrate_schema()` and the startup seeding come out of `app.py`. MySQL DDL is not transactional, so each revision is small and safe to rerun. Existing index names are looked up with the inspector, because `create_all` named them after columns.

SQLite cannot alter a column or drop a constraint in place, and the gate runs migrations on SQLite. So `migrations/env.py` sets `render_as_batch=True` and every revision that alters or drops (`0003` to `0008`) uses `op.batch_alter_table(...)`. The same revision files run on both databases. `batch_alter_table` keeps Alembic's default `recreate='auto'`, never `'always'`: SQLite rebuilds the table, MySQL issues a plain `ALTER TABLE`, which avoids foreign-key errors when altering parent tables such as `customers` and `inventory_items`.

Constraint names: `db = SQLAlchemy(metadata=MetaData(naming_convention=...))` from step 1, and `0001_baseline` names every constraint and index explicitly, because SQLite batch rebuilds fail on unnamed constraints. Databases created earlier by `create_all` keep their old auto-generated names, which is why `0003` looks names up with the inspector. `env.py` turns `PRAGMA foreign_keys` off for the duration of a SQLite migration run so parent tables can be rebuilt.

1. `0001_baseline`: today's schema exactly. Existing databases are stamped, new ones run it.
2. `0002_tenants_expand`: create `tenants`; add nullable `tenant_id` to all 18 tables. If any of those tables holds rows, insert one default tenant and backfill to it: name from `system_config.business_name` or `'Default Business'`, vertical `pharmacy` if `business_type` is pharmacy, otherwise `distributor` (also the fallback when `system_config` is empty). A completely empty database gets no default tenant.
3. `0003_tenants_contract`: normalise existing phone numbers to `254XXXXXXXXX` (rows that cannot be parsed or that collide after normalising are listed and the migration stops, nothing is guessed); `tenant_id` NOT NULL with FK and index on every table except `audit_logs` and `users`, which stay nullable (FK and index only; `before_flush` enforces non-null for ordinary users); swap the unique constraints in the table above; drop `system_config`.
4. `0004_billing`: `invoices`, `payment_intents`, `payments`, `payment_allocations`, `invoice_adjustments`. Each customer with `credit_balance > 0` gets one opening-balance invoice, then `credit_balance` is dropped.
5. `0005_batches`: `batches`, `item_variants`, `line_batches`, `inventory_items.has_variants`. One batch per existing item from its `batch_number` (or `LEGACY`), `expiry_date` and `stock`; then drop the two item columns.
6. `0006_pharmacy`, `0007_distributor`, `0008_institution`, `0009_sms`: the vertical tables and columns above.

Every revision has a tested downgrade. Env Daraja keys move into the default tenant with `flask tenants import-env-credentials`, not in a migration, so migrations never read secrets. My assumption stands: no production data needs preserving, but the path above keeps it anyway.

### Payments (`services/payments/`)

- `mpesa_service` takes a tenant: credentials from the tenant row, `CustomerPayBillOnline` or `CustomerBuyGoodsOnline` from `daraja_shortcode_type`, callback URL built from the slug.
- OAuth token cache: Redis key `daraja:token:{tenant_id}`, TTL `int(expires_in) - 120` seconds (Daraja returns `expires_in` as a string), shared by all gunicorn workers so they do not each hammer the token endpoint. Falls back to an in-process dict when Redis is unreachable, the same pattern as `services/otp_service.py:38`.
- Webhook routes. Daraja's URL registration refuses URLs containing words such as "mpesa" or "safaricom", so the paths differ from the original brief:
  - `POST /api/payments/daraja/stk/<tenant_slug>/<callback_token>` (STK result)
  - `POST /api/payments/daraja/c2b/<tenant_slug>/<callback_token>` (Paybill confirmation)
  - `POST /api/payments/daraja/c2b/<tenant_slug>/<callback_token>/validate`
- Exact response bodies, always HTTP 200 with JSON, including for duplicates and unmatched payments (anything else makes Daraja retry):
  - STK result: `{"ResultCode": 0, "ResultDesc": "Accepted"}`
  - C2B confirmation: `{"ResultCode": 0, "ResultDesc": "Confirmation received successfully"}`
  - C2B validation: `{"ResultCode": 0, "ResultDesc": "Accepted"}`. Validation never rejects: an unknown account reference is accepted and lands in the review queue, because turning away a parent's money is worse than matching it by hand.
  - A wrong `callback_token` or unknown slug returns 404 and records nothing.
- STK: pushing creates a `payment_intents` row, with the invoice number as AccountReference. No `Payment` exists until money is confirmed.
- One prompt at a time: a new push to a phone with an intent still pending under 90 seconds old is refused with "check your phone for the M-Pesa prompt". If Daraja itself answers a push with `500.001.1001` (subscriber busy), the page says to wait a minute and retry, and the invoice is not marked failed.
- STK takes whole shillings only. The push amount is the balance rounded up to the next shilling; the cents over stay on the payment as `unallocated_amount`.
- One payment, two webhooks: on a Paybill with C2B registered, an STK payment also fires the C2B confirmation, in either order, with the same receipt number. Both handlers call one `record_payment(tenant, receipt, amount, ...)`: insert on (tenant_id, mpesa_receipt); on a duplicate-key error, load the existing row and fill in what is missing. Allocation runs once, inside a transaction that locks the invoice row. The STK handler links its intent by `checkout_request_id`. The C2B handler needs no intent: its `BillRefNumber` is the invoice number we sent.
- Not done: linking by "same phone and amount within 10 minutes". C2B masks the phone, and two parents paying the same uniform price in the same minute would be cross-linked.
- Missed callbacks:
  - STK: any intent still `pending` after 90 seconds is checked with Daraja's STK query. "Still processing", which Daraja reports as error `500.001.1001` on the query, keeps it pending and updates `last_queried_at`; only a terminal answer (paid, cancelled, wrong PIN, timed out) closes it. An intent with no terminal answer after 10 minutes becomes `timeout`. Three triggers: the pay page polls its own status, a "Check payment" button, and `flask payments sweep` on a 10-minute cron (Heroku Scheduler's minimum).
  - Paybill: `org_account_balance` is stored from each C2B confirmation. If the balance rose by more than the payment since the previous one, the dashboard shows "possible missed payment" with the amount. It is a hint, not an alarm: withdrawals and bank sweeps lower the balance and are ignored, and out-of-order delivery can trip it.
- Manual code entry (`mpesa_manual`): staff type the 10-character M-Pesa code to close an invoice while Daraja go-live is pending or after a missed callback. Stored with `recorded_by` and `verified=false`. The same unique key applies, so a code cannot be used twice. When Daraja later reports that receipt:
  - Amounts equal: the row flips to verified.
  - Amounts differ (staff typed 5,000, Daraja says 500): Daraja's amount wins. The typed figure moves to `claimed_amount`, `amount` becomes Daraja's, allocations are redone against the real amount (invoices reopen if short), and an admin alert names the staff member. The row is verified at the true amount, never at the typed one.
  - Unverified totals show per staff member on the dashboard.
- C2B matching, in order: normalised `BillRefNumber` equals an invoice number; else a `members.member_ref`; else a `customers.account_ref`. For a member or customer match, the money is spread oldest invoice first.
- Allocation: pay down the target up to its balance; any excess rolls to that payer's next oldest open invoice; anything left stays as `unallocated_amount`. No match at all means `match_status=unmatched` and a row in a review queue with a one-click "assign to invoice" action.
- Forged callbacks: slugs are guessable, so webhook URLs carry the tenant's `callback_token` as a path segment (not a query string, which Daraja registration may strip), checked in constant time. The token segment is redacted in request logs. Rotating a token takes effect at once for STK (the URL is sent with each push) and reruns `register-c2b` for Paybill.
- STK callback handling never blocks or fails the webhook: the raw callback is saved on the intent first, then the handler returns 200. Verification with Daraja's status query uses a 5-second timeout. Verified: the payment is recorded. Query unavailable: the intent is marked `callback_received` and the sweep retries, so money is delayed, never lost and never taken on an unverified callback.
- Till tenants: STK uses the store number as `daraja_shortcode` and the till as PartyB. C2B registration is off by default for tills, since till payments carry no account reference and would only fill the review queue. If a till tenant turns it on and the C2B confirmation lands before the STK callback, the STK callback finds the unmatched payment by receipt and allocates it to the intent's invoice. Rule: whenever an upsert finds `unallocated_amount > 0` and now knows the invoice, allocation runs.
- Amount-mismatch alert: a dashboard query for `claimed_amount IS NOT NULL AND claimed_amount != amount`, plus an `AuditLog` entry. No separate alerts table.
- Optional Daraja IP allowlist, off unless `DARAJA_ALLOWED_IPS` is set. Behind Heroku the real client IP is only visible with `ProxyFix(app.wsgi_app, x_for=1, x_proto=1)`, which `create_app()` enables (it is commented out today at `app.py:40-42`). Without it the allowlist would block every webhook.
- `flask tenants register-c2b <slug>` registers the tenant's confirmation and validation URLs with Daraja, always with `"ResponseType": "Completed"`, so a slow or sleeping dyno can never cause Safaricom to refuse a customer's payment.

### Reversals and corrections

- Reallocate: from the review queue or any payment, void an allocation and assign the money to another invoice (wrong admission number). Both invoices' `amount_paid` and `status` are recomputed from live allocations. Admin role, audited.
- Payment reversed by Safaricom: admin marks the payment `reversed`; its allocations are voided and the invoices reopen. Manual, because Daraja does not reliably send a webhook for reversals.
- Returns and rejected deliveries: an `invoice_adjustments` row lowers the invoice, stock goes back to its batch, containers are adjusted.
- Credit note on an invoice already paid: balance never goes negative. The overpaid part is released by voiding that much allocation, newest allocation first, which returns it to the payment's `unallocated_amount`. The normal allocation routine then rolls it onto the payer's next open invoice, or it stays as visible customer credit. Example: 1,000 invoice paid in full, 200 returned, the duka has 200 credit.
- Voiding a controlled-drug sale (existing manager-OTP flow, `api/pos.py:631`): writes a new ledger row with `entry_type='adjustment'`, `reverses_entry_id` set, the manager as witness, and restores the batch quantity. The original row is never touched.

### Concurrency and stock reservation

- Stock changes lock rows with `with_for_update()`, always in ascending id order to avoid deadlocks: `InventoryItem`, then `Batch` (FEFO), then `ItemVariant`.
- Ledger sequence: lock the drug's `InventoryItem` row, read the last entry, write `seq + 1` with the chained opening balance. Unique (tenant_id, inventory_item_id, seq) is the backstop. Locking the parent row avoids MySQL gap-lock surprises from locking "the latest ledger row".
- When stock leaves the shelf:
  - Counter sale: at completion, as today.
  - Public pay-now order (refill, campaign): reserved at STK initiation. Stock is decremented then, with a `reserve` stock movement, and `orders.reserved_until` is set 15 minutes ahead (an STK prompt lasts about a minute). The first confirmed payment, full or installment, extends the hold to the campaign's `closes_at`, not forever. An order still part-paid at close releases its stock and the money paid shows as credit for the admin to refund or reassign.
  - Distributor credit order: at order confirmation, since no payment is awaited. A distributor order paid with "Pay now with M-Pesa" is a pay-now order and follows the reservation rule above instead.
- Abandoned pay-now invoices: when a reservation is released its invoice becomes `expired`. Expired and pay-now invoices have no `due_date`, are left out of credit exposure, deni aging and overdue reminders, so an abandoned checkout never looks like debt. A late payment quoting that invoice number reopens it and allocates as normal.
- Credit ceiling: placing a credit order locks the `customers` row, then checks exposure, defined as open invoice balances minus the customer's unallocated payment credit, plus the new order, against `credit_limit`. Two simultaneous orders cannot both slip under the ceiling.
- Abandoned reservations are released (stock restored, `release` movement) by the 10-minute sweep, and also on the spot when a checkout finds an item sold out.
- A payment arriving after its reservation was released still records and allocates; the order is flagged "paid, restock needed" on the dashboard instead of failing.

### Onboarding CLI

`flask tenants import-csv <slug> <type> <file.csv>` for `items`, `customers`, `members`, `tier_prices`. Validates every row first, normalises phones, prints rejected rows with reasons, and imports nothing if `--strict` is set and any row fails. Rerunning updates by SKU, phone or member ref instead of duplicating.

### Public links (`public/` blueprint, mobile-first Bootstrap)

- Institution: `/o/<slug>/c/<campaign_slug>`. Enter admission or house number, pick items and sizes, pay in full or an installment, STK push. Shareable to a WhatsApp group. Before the STK push the page shows a masked confirmation, for example "Brian O., Form 1 East", so a mistyped admission number is caught. Admission numbers are often sequential, so the lookup needs two things: the number and the child's first name (case-insensitive). A wrong pair returns the same "not found" as an unknown number. Only first name, last initial and class are shown, and lookups are rate-limited.
- Pharmacy: `/o/<slug>/r/<refill_token>`. Confirm the refill, pick delivery address or pickup, STK push. A prescription containing any `is_controlled` item cannot be paid or dispatched from the link: the page offers "Ask the pharmacist to call me", which queues a request on the dashboard, and its reminder SMS says to visit or call. The counter rules for controlled drugs are not bypassed by a URL.
- Distributor: `/o/<slug>/d/<public_token>`. Catalog at the duka's tier price, current balance and ceiling shown; an order that would pass the ceiling cannot go on credit, but the same screen offers "Pay now with M-Pesa" for that order by STK push. Only more deni is refused, never the sale.
- Pay link for any invoice: `/o/<slug>/pay/<pay_token>`. SMS cannot start an STK push by itself, so reminders link here and the page has the "Pay now" button.
- Tokens are random and stored (revocable), never sequential ids. Public POSTs are rate-limited with Flask-Limiter on the existing Redis, so nobody can spam STK prompts to strangers.

### Dashboards (`verticals/<name>/`: routes, templates, tests each)

`/` goes to the tenant's vertical dashboard. The POS, shift, inventory and reports screens stay in the nav, tenant-scoped.

- Pharmacy: expiry radar with 30/60/90-day buckets from `batches`; controlled-drug register with running balance and a PPB inspection PDF; refill schedule (due today, next 7 days, overdue).
- Controlled drugs at the counter (`api/pos.py`): an `is_controlled` item can only be added by a pharmacist (or with a pharmacist OTP through the existing override flow). Checkout is refused unless prescriber name and reg number, patient name, National ID and address are present, from a linked prescription or typed at the till. The stock change and the ledger row are written in one transaction, so a controlled sale without a complete register entry cannot exist.
- Distributor: deni aging by days past `due_date` (not yet due, 0-7, 8-14, 15+), per-duka container balance, "Send STK reminder" per invoice, eTIMS badge. Until live submission exists the badge reads "Not fiscalised", or shows the pasted `external_etims_ref`; nothing on screen or on a receipt implies KRA compliance.
- Institution: per-campaign paid versus pending and size breakdown, uncollected-but-paid packing list, pickup verification screen.
- Pickup: a 6-digit code goes by SMS only when the invoice is paid in full, never on an installment, with a QR of the same code. Verification refuses an order whose invoice is not `paid`, and is one atomic `UPDATE ... WHERE picked_up_at IS NULL`, so a second scan fails and shows who collected and when.

### SMS and scheduler

- `services/sms_service.py`: one `send(tenant, to, body, kind, dedupe_key)` that writes `sms_messages` and uses the tenant's `at_sender_id`. All tenants send through one platform Africa's Talking account and differ only by sender id.
- `/delivery-reports` (`app.py:529`) is one global URL (Africa's Talking sets it per account, so it cannot carry a slug). It runs in `unscoped()`, finds the row by `provider_message_id` (unique index), and only moves status forward, so a late "Sent" cannot overwrite "Delivered".
- Dates: "today" is always the Nairobi calendar date, from one helper `services/clock.py: today_nairobi()`, used by the scheduler, FEFO and aging. Heroku runs on UTC, three hours behind.
- `services/scheduler.py`: `run_daily_triggers(today)` loops active tenants, binds each, and runs:
  - Refill reminders: active chronic prescriptions where `refill_due_date == today + 4 days`, with the refill link.
  - Overdue credit reminders: invoices with a balance past `due_date`, with the pay link.
- Run as `flask run-daily-triggers` from Heroku Scheduler or cron. No in-process APScheduler: gunicorn workers would each fire the job.
- Each tenant runs inside its own try/except with rollback and `expunge_all()`, so one tenant's failure does not stop the rest. Trial and active tenants get triggers; past-due and cancelled do not.
- Dedupe keys carry the stage (`overdue:<invoice>:d1`, `d3`, `d7`, `d14`, then `w<ISO week>`), so a same-day rerun sends nothing but the next stage still goes out.
- My call on cadence: overdue reminders go on days 1, 3, 7 and 14 past due, then weekly. A text every day gets the sender blocked.

### Code layout

- `models.py` becomes a `models/` package (`base`, `core`, `billing`, `pharmacy`, `distributor`, `institution`) that re-exports every name, so `from models import X` keeps working.
- `app.py` gets `create_app()`; `app = create_app()` stays for `Procfile`. Tests build the app on SQLite without touching MySQL.
- SQLAlchemy moves from 2.0.36 to the newest 2.0.x on PyPI at step 1, pinned `>=2.0.55,<2.1`. The review cites tenant-filter fixes in 2.0.55; that is checked against the changelog at step 1, not taken on trust.
- Flask-Limiter storage is `memory://` under test or when `REDIS_URL` is unset, and runs with `swallow_errors=True` in production, so a Redis outage weakens rate limiting but never breaks a checkout link.
- Declined from the review: OSCU/VSCU columns on `tenants`, a UNSPSC code on items, and eTIMS fields on credit notes. They belong to the eTIMS project, are cheap nullable columns to add then, and guessing KRA's field list now would be wrong in detail.
- New libraries: ReportLab (PPB PDF with repeating table headers; pure Python, no system packages on Heroku), segno (QR, pure Python), Flask-Limiter.

## Tests and evidence

- Isolation: two seeded tenants. Every GET/PUT/DELETE route is hit as tenant B with tenant A's ids and must 404. A model test asserts every mapped class uses `TenantMixin` or is on a one-item allowlist. An unbound query raises. Cross-tenant insert raises. Same SKU and same phone in two tenants both succeed.
- Tenancy hooks: a bulk `update()` and a bulk `delete()` as tenant B leave tenant A's rows untouched; the same query run as A then B in one process returns each tenant's own rows (catches a cached tenant id); `session.get()` for A's id after switching to B returns nothing.
- Filter bypass ban: a static test fails on `text(`, `.__table__`, `db.engine`, `.connection(`, `bulk_insert_mappings`, `bulk_update_mappings`, `insert(` and `db.metadata.tables` anywhere outside `migrations/` and `models/`. Runtime tests: a Core statement and an ORM `insert()` through `session.execute()` both raise; a `union()` query run as A then B returns each tenant's own rows.
- Migration round trip on a file-backed SQLite database (`tmp_path`) with foreign keys on outside the migration, across every revision that exists at that step.
- STK: a 100.50 balance pushes 101 and leaves 0.50 unallocated; a "still processing" query keeps the intent pending; a query outage returns 200 and the sweep later records the payment; till C2B before STK ends matched.
- Abandoned checkout: after release the invoice is `expired`, absent from exposure, aging and reminders; a late payment reopens and pays it. A second push to the same phone inside 90 seconds is refused. A `500.001.1001` on push leaves the invoice open.
- Settings: stored secrets never appear in any response body.
- Batches: an expired earlier batch is skipped; a 10-unit sale over two batches writes two `line_batches` rows and a void restores each batch exactly.
- Credit: two concurrent orders against one ceiling, one is refused (MySQL lane).
- Pharmacy: a controlled prescription cannot be paid through a refill link; the ledger keeps the pharmacist's reg number after the user record changes.
- Institution: wrong first name gives "not found"; a part-paid order gets no pickup code and cannot be collected; stock releases at campaign close.
- SMS: a delivery report with no session updates the right row, and a late "Sent" does not downgrade "Delivered".
- Scheduler at 22:30 UTC uses the next day's Nairobi date; one failing tenant does not stop the others.
- Migrations: an empty database upgrades with no default tenant; a database with rows but an empty `system_config` gets the fallback tenant.
- Manual entry: lower-case code collides with the upper-case webhook; typed 5,000 against a real 500 ends at 500, invoice reopened, alert raised.
- Credit note after full payment leaves balance at zero and 200 unallocated, which then pays down the next invoice.
- Controlled sale: cashier role refused; missing patient field refused; a successful sale has exactly one ledger row.
- Public links: masked name shown, full name never in the response; over-limit duka can complete by STK.
- Proxy: with `X-Forwarded-For` set, the allowlist sees the forwarded IP.
- Stock invariant: after any sequence of sales, restocks, reservations and releases, `inventory_items.stock` equals the sum of its variants or batches; a grep test fails on any assignment to `.stock` outside `services/stock.py`.
- Webhooks: each handler's response body is asserted byte for byte; no registered URL contains "mpesa" or "safaricom".
- Token cache: two calls for one tenant fetch one token; two tenants get separate tokens; Redis down falls back without error.
- Reconciliation: exact pay, partial then remainder, overpay rolling to the next invoice, one payment across several invoices by admission number, unknown reference to the review queue, duplicate Daraja callback counted once, bad callback token rejected, payment for tenant A posted to tenant B's URL rejected.
- Double webhook: STK callback then C2B, and C2B then STK, for one receipt, each end with one payment and one allocation. Manual code followed by the Daraja webhook for the same receipt ends with one verified payment.
- Reversals: reallocate between two invoices and check both balances; reverse a payment and the invoice reopens; void a controlled sale and the ledger shows an offsetting row with the original unchanged and balances still chained.
- Concurrency (MySQL lane in CI, since SQLite ignores row locks): two threads sell the last unit, one wins; 20 threads append ledger rows, `seq` is gapless; reservation expiry restores stock exactly once.
- Phones: `0712...`, `+254712...`, `254712...`, `0110...` all store the same value; a second format of an existing number hits the unique constraint.
- CSV import: bad rows reported, rerun is idempotent.
- Scenario table at `tests/evals/reconciliation_cases.json` (payload in, ledger out) as the regression set. No LLM calls in this build, so there are no paid evals.
- Scheduler: fixed `today`, asserts who gets a message, and that a second run sends nothing.
- Migrations: upgrade and downgrade on SQLite in the gate; on MySQL 5.7 in CI.
- Traces: `payments.match_status` gives the auto-match rate per tenant; `sms_messages` gives delivery rate; a log line per daily run with counts per tenant.

## Build order

1. App factory, Alembic baseline, test harness on SQLite.
2. Tenancy: models package, mixin, hooks, decorators, signup, migrations 0002-0003, isolation tests.
3. Billing and M-Pesa: migration 0004, per-tenant Daraja client, callbacks, reconciliation tests.
4. Batches (0005), SMS outbox (0009), public link shell with tokens and rate limits.
5. Pharmacy, distributor and institution, each in its own worktree in parallel (0006-0008).
6. Scheduler, then README, `.env.example`, docs.

Commit and push per green step, on branch `feat/vertical-saas`.

## Risks to check during the build

- Safaricom masks the payer phone in C2B confirmations, so Paybill matching relies on the account reference, not the phone. Alternate payer numbers help STK only. Check against current Daraja docs.
- Till (Buy Goods) payments made outside STK carry no account reference. Till tenants reconcile through STK; institutions should be onboarded on a Paybill.
- The tenancy hook tests above run in step 2 before anything else builds on the filter. Raw `text()` SQL bypasses the filter; a test bans it outside migrations.
- Manual code entry lets staff close an invoice with a made-up code. The unverified flag, per-staff totals and the audit log are the control; tenants can switch the mode off once Daraja is live.
- That an STK payment on a Paybill also fires the C2B confirmation with the same receipt is confirmed in the Daraja sandbox in step 3. The upsert design is correct either way.
- Patient names and medicines are visible to anyone holding a refill link. The token is the only secret.

## On approval

1. Create branch `feat/vertical-saas`, save this design to `docs/superpowers/specs/2026-11-24-vertical-saas-pivot-design.md`, commit and push.
2. Build step 1 only, then stop for review:
   - `create_app(config_override=None)` in `app.py`, with `app = create_app()` kept at the bottom for `Procfile`. Importing `app` or building a test app on `sqlite:///:memory:` must not open a MySQL connection (`create_database()`, `db.create_all()`, `migrate_schema()` and seeding leave import time).
   - `create_app()` opens no MySQL or Redis connection; Africa's Talking initialises lazily (`services/sms_service.py:14` does it at import today).
   - `ProxyFix` inside `create_app()`, hop counts from config `PROXY_FIX_X_FOR` and `PROXY_FIX_X_PROTO`, default 1.
   - `MetaData(naming_convention=...)` on `db`. SQLAlchemy upgraded and pinned as described above.
   - Flask-Migrate initialised, `migrations/env.py` with `render_as_batch=True` and the SQLite foreign-key toggle (`PRAGMA foreign_keys=OFF` issued on the connection before `context.begin_transaction()`, because SQLite ignores it inside a transaction), and `0001_baseline` matching the current 19 tables exactly with every constraint named.
   - `tests/conftest.py` with a shared app fixture, plus a test that runs upgrade then downgrade on a file-backed SQLite database in `tmp_path`. Existing test files switch to the shared fixture.
   - Run pytest; all existing tests and the round-trip test pass; commit and push.
3. Steps 2 to 6 follow after review, each from a task-level plan.
