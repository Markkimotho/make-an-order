# Step 5: Remote orders and the three verticals

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A business can take orders and money without a login on the customer's side, in the way its trade works: a pharmacy dispenses controlled drugs only with a complete register entry and reminds chronic patients to refill; a distributor sells to dukas on credit up to a ceiling, at their price tier, and tracks containers; a school, estate or chama runs a campaign that parents pay for in full or in instalments and collect with a code.

**Spec:** `docs/superpowers/specs/2026-11-24-vertical-saas-pivot-design.md` (Schema; Concurrency and stock reservation; Public links; Dashboards; Reversals).

## Global constraints

- Every public page follows `public/routes.py`: business from the exact slug, record from a random stored token, `404` otherwise, `no-store`/`no-referrer`/`noindex`, tokens redacted from logs, POSTs rate-limited, STK prompts capped per invoice under the invoice lock.
- Every remote order has an invoice (`invoices.order_id`). Stock for a pay-now order is taken when the M-Pesa prompt is sent (`stock.take(order_item=...)`) and held until `orders.reserved_until`; an unpaid hold is released by the sweep (stock back to its batches, invoice `expired`). Paid orders are never released. A payment on an expired invoice reopens it; the order is flagged `restock_needed` if its stock went back.
- Stock still changes only through `services/stock.py`. A controlled drug's stock cannot change without a register entry: `services/stock.py` refuses it unless the caller passes the entry.
- Lock order, everywhere: tenant billing lock, then sale/order/customer row, then invoice, then stock (item, batches by id, variant), then the controlled register (by item).
- Money and quantities stay `Decimal`/integers; Nairobi dates from `services/clock.py`.

## Decisions (Mark can overturn)

- Built in this session one vertical after another, not in parallel worktrees: the three share `orders`, `services/stock.py` and the migration chain, so parallel branches would collide on revision numbers and on the same files. Each vertical lives in `verticals/<name>/` (routes, templates, service, tests) so later work can still split by directory.
- Revision numbers: `0007_remote_orders`, `0008_pharmacy`, `0009_distributor`, `0010_institution`.
- The daily SMS triggers (refill day 26, overdue credit) are step 6; this step builds what they link to (refill link, pay link) and the queries they will call.
- PPB register PDF uses ReportLab (pure Python). Its column set is provisional until checked against a real register with a pilot pharmacist (spec risk).
- Pickup codes are 6 digits, stored hashed (HMAC with `APP_SECRET_KEY`), sent only when the invoice is fully paid.

## Tasks

### Task 1: Remote orders and reservations (`services/orders.py`, 0007)
- `orders`: `order_number` (per tenant), `channel` (staff/public_link), `fulfillment_status` (pending/ready/dispatched/collected/released/restock_needed/cancelled), `delivery_address`, `reserved_until`, `prescription_id`, `campaign_id`, `member_id`, `pickup_code_hash`, `picked_up_at`, `picked_up_by`. `order_items.variant_id`. `invoices.member_id`.
- `orders.create_order(customer_id|member_id, lines, channel, pay_now, ...)`: validates lines, prices, creates Order, OrderItems, Invoice (`kind="sale"` with no due date for pay-now; `kind="credit"` with due date otherwise); takes stock (pay-now: with `reserved_until = now + 15 min`).
- `orders.release_expired(now)`: unpaid holds past `reserved_until` give stock back and expire the invoice; called by `flask payments-sweep`. `orders.extend_hold(order, until)` for instalment campaigns.
- Late payment: after allocation, an order whose stock was released is marked `restock_needed`.
- Tests: hold, release, payment before release keeps stock, late payment reopens + flags, release exactly once (MySQL), expired invoices excluded from exposure and aging.

### Task 2: Pharmacy (0008, `verticals/pharmacy/`)
- `inventory_items.drug_schedule`, `is_controlled`; `users.ppb_reg_no`; prescription refill fields (`prescriber_reg_no`, `is_chronic`, `duration_days`, `last_dispensed_at`, `refill_due_date`, `refills_remaining`, `refill_status`, `refill_token`).
- `controlled_drug_ledger` (append-only, `seq` per item, chained balances, snapshots of patient, prescriber and pharmacist); `services/controlled.py`; stock writes on controlled items require a register entry.
- POS: controlled items added only by a pharmacist; checkout needs prescriber name and reg no, patient name, National ID, address; one ledger row per batch taken; void writes offsetting rows.
- Dashboard: expiry radar (30/60/90 from batches), register with running balance and PPB PDF, refill schedule (due today, next 7 days, overdue).
- Public refill link `/o/<slug>/r/<refill_token>`: confirm, delivery or pickup, STK; prescriptions with a controlled item show "Ask the pharmacist to call me" instead.
- Tests per spec list (cashier refused, missing field refused, one row per batch, chained seq, void offset, reg no survives user change, controlled refill cannot be paid).

### Task 3: Distributor (0009, `verticals/distributor/`)
- `customers`: `account_ref`, `public_token`, `national_id`, `physical_address`, `kra_pin`, `route_zone`, `gps_lat`, `gps_lng`, `price_tier_id`; `price_tiers`, `tier_prices`, `customer_phones`, `container_movements`; `inventory_items.is_returnable_container`.
- Credit orders lock the customer, check exposure + order against `credit_limit`; over the ceiling, the same order can be paid now by STK.
- Public duka link `/o/<slug>/d/<public_token>`: catalogue at the duka's tier price, balance and ceiling, order on credit or pay now.
- Dashboard: deni aging (not yet due, 0-7, 8-14, 15+), container balances, "Send reminder" per invoice (SMS with pay link), eTIMS badge ("Not fiscalised" or the pasted CUIN).
- C2B matching by `account_ref` already exists in billing; alternate payer phones used for STK lookups.
- Tests: ceiling refused, concurrent orders (MySQL), tier prices, container balance, aging buckets, reminder SMS dedupe.

### Task 4: Institution (0010, `verticals/institution/`)
- `members` (`member_ref`, `name`, `group_label`, `guardian_phone`, `secondary_phone`), `campaigns`, `campaign_items`.
- Public `/o/<slug>/c/<campaign_slug>`: member by ref + first name (same "not found" either way, rate-limited), masked confirmation, items and sizes, full or instalment (min instalment), STK. Hold extends to `closes_at` after the first payment; part-paid at close releases stock and leaves the money as credit.
- Pickup: code by SMS only when fully paid; verification is one atomic update, a second scan shows who collected and when.
- Dashboard: paid vs pending per campaign, size breakdown, packing list, pickup screen.
- Tests per spec list.

### Task 5: Landing per vertical, CSV import, docs, review
- `/dashboard` renders the tenant's vertical dashboard; POS, inventory, reports stay in the nav.
- `flask tenants import-csv <slug> <items|customers|members|tier_prices> <file>`: validate all rows, `--strict`, idempotent reruns.
- README, services README, `.env.example`; full suite on both databases; CI; Codex review; fix.
