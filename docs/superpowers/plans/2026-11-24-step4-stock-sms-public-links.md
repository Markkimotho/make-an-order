# Step 4: Batches and FEFO, SMS outbox, public link shell

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stock lives in batches (or variants) and leaves first-expiry-first-out, never from expired batches, through one writer; every SMS is a row with a delivery status and cannot be sent twice; customers can open a per-business public link and pay an invoice by STK push with no login.

**Architecture:** `services/stock.py` is the only code that changes stock (enforced by a source scan). `services/clock.py` gives "today" in Nairobi. `sms_messages` is an outbox written by `services/sms_service.send()`; Africa's Talking delivery reports update it through the global `/delivery-reports` URL. `public/` is a blueprint under `/o/<slug>` that binds the business from the slug, uses random stored tokens, and is rate-limited with Flask-Limiter.

**Spec:** `docs/superpowers/specs/2026-11-24-vertical-saas-pivot-design.md` (Schema: batches, line_batches, item_variants, sms_messages; Concurrency and stock reservation; SMS and scheduler; Public links).

## Global Constraints

- `inventory_items.stock` = sum of its batches (or variants when `has_variants`); written only by `services/stock.py`.
- Lock order: `InventoryItem`, then its `Batch` rows in id order, then `ItemVariant`.
- FEFO skips batches whose `expiry_date` is before `today_nairobi()`; expired stock leaves only through an adjustment or write-off.
- Selling more than the sellable stock is refused (409); the old code clamped stock to 0 and sold anyway.
- Every SMS has a row; `(tenant_id, dedupe_key)` unique; delivery status only moves forward.
- Public routes: random stored tokens only, never sequential ids; unknown slug or token is 404; POSTs rate-limited; Redis outage never breaks a page (`swallow_errors`).

## Decisions (Mark can overturn)

- Revision numbers follow build order: `0005_batches`, `0006_sms_outbox`. The vertical tables come after.
- Stock reservation for unpaid public orders (`orders.reserved_until`) needs the order flow, so it lands with the verticals (step 5); this step builds the pay-an-invoice link only.
- Items edited through the old form: batch number and expiry apply to the item's only batch; an item with several batches is edited per batch (`PUT /inventory/batches/<id>`).
- `tzdata` is added to requirements so `Africa/Nairobi` resolves on slim servers.

## Tasks

### Task 1: Batches, variants, FEFO and the single stock writer
- `models/stock.py`: `Batch`, `ItemVariant`, `LineBatch` (`transaction_item_id?`, `order_item_id?`, `batch_id`, `quantity`, `returned_quantity`); `InventoryItem.has_variants`; `batch_number`/`expiry_date` columns removed, with read-only properties giving the soonest-expiring stocked batch for old screens.
- `0005_batches`: tables; one batch per item with stock, from its batch number (or `LEGACY`) and expiry; drop the two columns. Downgrade puts the soonest batch back on the item.
- `services/clock.py: today_nairobi()`.
- `services/stock.py`: `receive(item, qty, batch_number=None, expiry_date=None, unit_cost=None, variant=None, ...)`, `take(item, qty, line=None, variant=None, allow_expired=False, ...) -> [(batch, qty)]`, `give_back(item, qty, line=None, ...)`, `adjust(item, delta, ...)`, `InsufficientStock`.
- Rewire every writer: POS complete/void/refund, shop order and its cancel/delete, restock, stock adjustment, PO receive, item create/update, seeding; expiry reports read batches; `GET /inventory/items/<id>/batches`, `PUT /inventory/batches/<id>`.
- Tests: migration with data; FEFO order; expired skipped; multi-batch sale writes two `line_batches` and a void restores each batch exactly; partial refund returns to the batch it came from; oversell 409; stock == sum(batches) after a random sequence; source scan bans stock writes elsewhere; concurrent last-unit sale (MySQL).

### Task 2: SMS outbox and delivery reports
- `sms_messages`: `to_phone`, `body`, `kind`, `dedupe_key?`, `status` (`queued|sent|failed|skipped`), `provider_message_id?` (unique), `delivery_status?`, `error?`, `created_at`, `sent_at?`, `delivered_at?`.
- `services/sms_service.send(to, body, kind, dedupe_key=None) -> SmsMessage` in the bound tenant: dedupe returns the existing row; bad phone -> `skipped`; tenant's `at_sender_id`; provider id and status from Africa's Talking's reply. `SendSMS.*` helpers go through it.
- `/delivery-reports`: unscoped lookup by provider id; final states (`Success`, `Failed`, `Rejected`) are never overwritten by a late `Sent`/`Submitted`/`Buffered`.
- Tests: row per send; dedupe; skipped phone; AT outage -> failed, caller unaffected; delivery report updates right row across tenants; late downgrade ignored; unknown id ignored.

### Task 3: Public link shell and invoice pay page
- `public/` blueprint `/o/<slug>`: `public_tenant` binding (unknown slug or lapsed business -> 404), `GET /o/<slug>/pay/<pay_token>`, `POST .../stk` (`phone`), `GET .../status?intent=<id>`.
- Flask-Limiter: storage `RATELIMIT_STORAGE_URI` (Redis when `REDIS_URL` is set, else memory), `swallow_errors=True`; 5 pushes/minute per IP, 3 pushes per invoice per 10 minutes.
- `services/billing.pay_url(invoice)` for SMS reminders.
- Mobile-first page: business name, invoice number, lines, paid, balance; phone and Pay button; polls status; paid state.
- Tests: page by token; wrong token / other business's slug -> 404; no login needed; STK from the page; rate limits; Redis down still serves; intent from another invoice -> 404; paid invoice shows paid and no button; sweep covers `/o/` routes.

### Task 4: Docs, review
- README sections; services README; `.env.example`; full suite both databases; CI; Codex review; fix.
