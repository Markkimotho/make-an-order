# make-an-order

A customer and order management system with role-based access control (RBAC), a live inventory/shop, and SMS notifications via Africa's Talking.

**Base URL (local):** `http://127.0.0.1:5001`

---

## Table of Contents

- [Overview](#overview)
- [Tech Stack](#tech-stack)
- [Roles & Access](#roles--access)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Running the App](#running-the-app)
- [API Reference](#api-reference)
  - [Authentication Endpoints](#authentication-endpoints)
  - [Customer Endpoints](#customer-endpoints)
  - [Order Endpoints](#order-endpoints)
  - [Inventory Endpoints](#inventory-endpoints)
  - [POS Endpoints](#pos-endpoints)
  - [Payment Endpoints](#payment-endpoints)
  - [Pharmacy Endpoints](#pharmacy-endpoints)
  - [Reports Endpoints](#reports-endpoints)
  - [User Management Endpoints](#user-management-endpoints)
  - [Common Errors](#common-errors)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [Testing](#testing)

---

## Overview

**make-an-order** has grown beyond a customer/order tracker into a full **Point of Sale (POS) system** for SMEs (pharmacies, supermarkets, hardware shops, salons, general retail). It now combines:

- Cashier-driven sales transactions with multiple payment methods (cash, M-Pesa STK, card via Flutterwave, store credit, split tender)
- Shift / till management with Z-reports
- Inventory with batches, expiry tracking, low-stock alerts, suppliers and purchase orders
- Pharmacy module: prescription capture and controlled-drug ledger
- Customer loyalty points and store credit
- Daily reports dashboard with CSV export

The legacy customer/order/SMS flows from earlier versions are preserved unchanged.

**Features:**
- Google OAuth 2.0 login + customer self-checkout flow
- Six roles (`superuser`, `admin`, `user`, `cashier`, `pharmacist`, `stock_clerk`)
- POS transactions with tax, discount, loyalty redemption, manager-OTP void
- M-Pesa Daraja STK Push + callback (Safaricom)
- Flutterwave card payments + webhook
- Africa's Talking SMS (order/payment/low-stock/prescription/credit-reminder)
- Receipts: SMS, thermal-printer (42-col), and HTML
- Manager-override OTP via Redis (with in-memory fallback)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Flask 3.0, SQLAlchemy 2.0 |
| Database | MySQL (local) / JawsDB (production) |
| Migrations | Flask-Migrate (Alembic) |
| Auth | Google OAuth 2.0 (Authlib) |
| Payments | M-Pesa Daraja, Flutterwave |
| SMS | Africa's Talking API |
| OTP store | Redis (with in-memory fallback) |
| Frontend | Jinja2, Bootstrap 5.3, Font Awesome 6, Chart.js 4 |
| Server | Gunicorn (production) |
| Testing | Pytest |

---

## Roles & Access

| Feature | `user` | `cashier` | `pharmacist` | `stock_clerk` | `admin` | `superuser` |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| View own orders / receipts | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Browse inventory / shop | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **POS** — open shift, sell, take payment | ✗ | ✓ | ✓ | ✗ | ✓ | ✓ |
| **Pharmacy** — capture & dispense prescriptions | ✗ | ✗ | ✓ | ✗ | ✓ | ✓ |
| **Stock** — purchase orders, receive, suppliers | ✗ | ✗ | ✗ | ✓ | ✓ | ✓ |
| Manage customers / orders / reports | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ |
| Manage users (roles, linking) | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |

Roles are stored on the `User` row and enforced by decorators in `auth/auth_middleware.py` (`cashier_required`, `pharmacist_required`, `stock_clerk_required`, `admin_required`, `superuser_required`, plus their `*_api_required` JSON variants).

**Superuser:** `kimothomark93@gmail.com` — enforced on every login regardless of the database.

**New users:** Any Google account that logs in for the first time is auto-created with the `user` role. A superuser must link them to a customer profile before they can place orders.

---

## Getting Started

### Prerequisites

- Python 3.8+
- MySQL running locally
- Google OAuth 2.0 credentials ([setup guide](https://support.google.com/cloud/answer/6158849?hl=en))
- Africa's Talking account (optional — SMS fails gracefully without it)

> **OAuth redirect URI:** Add `http://127.0.0.1:5001/authorize` as an Authorised redirect URI in your Google Cloud Console.

### Installation

**1. Clone the repo**

```bash
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order
```

**2. Create and activate a virtual environment**

```bash
python -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Configure environment variables**

Create a `.env` file in the project root:

```env
# Google OAuth
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
APP_SECRET_KEY=a-very-strong-random-secret-key

# MySQL (local)
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASSWORD=your_mysql_password
MYSQL_DB=customers_orders

# Africa's Talking (optional)
AT_USERNAME=sandbox
AT_API_KEY=your_at_api_key
AT_SENDER_ID=your_sender_id
```

### Database setup

The schema is managed by Alembic (Flask-Migrate). The app never creates or
alters tables on startup.

New database:

```bash
export FLASK_APP=app
flask create-db        # local MySQL only: CREATE DATABASE IF NOT EXISTS
flask db upgrade       # builds every table from migrations/versions/
```

Existing database created by an older version of this app (which built its
tables with `db.create_all()` on startup): mark it as already at the baseline
once, then upgrade normally from then on.

```bash
flask db stamp 0001_baseline
flask db upgrade
```

After pulling new code, always run `flask db upgrade`.

### Running the App

```bash
python app.py
```

The app starts at **`http://127.0.0.1:5001`**. The first admin login is sent
to onboarding, which records the business type and seeds sample inventory for
it.

---

## API Reference

All endpoints require an active session (Google OAuth login). API endpoints return JSON.

**Base URL:** `http://127.0.0.1:5001`

> **Postman tip:** Log in via the browser first to establish a session cookie, then use that cookie in Postman (`Cookie` header or Postman's cookie jar) to authenticate API requests.

---

### Authentication Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/login` | Redirects to Google OAuth login |
| `GET` | `/authorize` | OAuth callback (handled automatically) |
| `GET` | `/logout` | Clears session and redirects to login |

---

### Customer Endpoints

> Requires `admin` or `superuser` role. Returns `403` for regular users.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/customers/register` | Create a new customer |
| `GET` | `/customers/view_customers` | List all customers |
| `GET` | `/customers/view_customers/<id>` | Get a specific customer |
| `PUT` | `/customers/update_customers/<id>` | Update customer details |
| `DELETE` | `/customers/delete_customers/<id>` | Delete customer (cascades to orders) |

#### Create a Customer

```
POST http://127.0.0.1:5001/customers/register
Content-Type: application/json
```
```json
{
  "name": "Jane Doe",
  "phone_number": "+254712345678",
  "code": "CUST001"
}
```

`201 Created`
```json
{ "message": "Customer registered successfully", "customer_id": 1 }
```

`400 Bad Request`
```json
{ "error": "Phone number or code already exists" }
```

---

#### List All Customers

```
GET http://127.0.0.1:5001/customers/view_customers
```

`200 OK`
```json
[
  { "id": 1, "name": "Jane Doe", "phone_number": "+254712345678", "code": "CUST001" }
]
```

---

#### Get a Customer

```
GET http://127.0.0.1:5001/customers/view_customers/1
```

`200 OK`
```json
{ "id": 1, "name": "Jane Doe", "phone_number": "+254712345678", "code": "CUST001" }
```

`404 Not Found`
```json
{ "error": "Customer not found" }
```

---

#### Update a Customer

```
PUT http://127.0.0.1:5001/customers/update_customers/1
Content-Type: application/json
```
```json
{ "name": "Jane Smith" }
```

`200 OK`
```json
{ "message": "Customer updated successfully" }
```

---

#### Delete a Customer

```
DELETE http://127.0.0.1:5001/customers/delete_customers/1
```

`200 OK`
```json
{ "message": "Customer deleted successfully" }
```

---

### Order Endpoints

> Create / update / delete requires `admin` or `superuser`. Regular users can only call `/orders/my_orders`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/orders/place_order` | Place a new order (admin+) |
| `GET` | `/orders/view_orders` | List all orders (admin+) |
| `GET` | `/orders/view_orders/<customer_id>` | Orders for a specific customer (admin+) |
| `GET` | `/orders/my_orders` | Current user's own orders (all roles) |
| `PUT` | `/orders/update_orders/<id>` | Update an order (admin+) |
| `DELETE` | `/orders/delete_orders/<id>` | Delete an order (admin+) |

#### Place an Order

```
POST http://127.0.0.1:5001/orders/place_order
Content-Type: application/json
```
```json
{ "customer_id": 1, "item": "Laptop", "amount": 1299.99 }
```

`201 Created`
```json
{
  "message": "Order placed successfully!",
  "id": 5,
  "sms_status": "SMS sent successfully"
}
```

---

#### List All Orders

```
GET http://127.0.0.1:5001/orders/view_orders
```

`200 OK`
```json
[
  { "id": 5, "customer_id": 1, "item": "Laptop", "amount": 1299.99, "time": "2026-03-28T10:00:00" }
]
```

---

#### My Orders

```
GET http://127.0.0.1:5001/orders/my_orders
```

`200 OK` — same structure, filtered to the logged-in user's linked customer profile.

---

#### Update an Order

```
PUT http://127.0.0.1:5001/orders/update_orders/5
Content-Type: application/json
```
```json
{ "item": "Desktop PC", "amount": 999.00 }
```

`200 OK`
```json
{ "message": "Order updated successfully" }
```

---

#### Delete an Order

```
DELETE http://127.0.0.1:5001/orders/delete_orders/5
```

`200 OK`
```json
{ "message": "Order deleted successfully" }
```

---

### Inventory Endpoints

> Available to all authenticated users. Order placement is role-aware: admins select a customer; regular users order against their own profile automatically.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/inventory/items` | List all items (optional `?category=` filter) |
| `GET` | `/inventory/categories` | List distinct categories |
| `POST` | `/inventory/place_order` | Place a cart order from inventory |

#### List Inventory Items

```
GET http://127.0.0.1:5001/inventory/items
GET http://127.0.0.1:5001/inventory/items?category=Electronics
```

`200 OK`
```json
[
  {
    "id": 1,
    "name": "Laptop Pro 15\"",
    "description": "16 GB RAM · 512 GB SSD · Intel Core i7",
    "price": 1299.99,
    "category": "Electronics",
    "stock": 15,
    "icon": "fa-laptop"
  }
]
```

---

#### Place an Inventory Order

```
POST http://127.0.0.1:5001/inventory/place_order
Content-Type: application/json
```

Admin/superuser (must include `customer_id`):
```json
{
  "customer_id": 1,
  "items": [
    { "id": 1, "quantity": 2 },
    { "id": 7, "quantity": 1 }
  ]
}
```

Regular user (`customer_id` is ignored — taken from session):
```json
{
  "items": [{ "id": 3, "quantity": 1 }]
}
```

`201 Created`
```json
{
  "message": "2 line order(s) placed successfully!",
  "orders": ["Laptop Pro 15\" ×2", "Ergonomic Chair"]
}
```

`400 Bad Request`
```json
{ "error": "Only 1 units of \"Laptop Pro 15\"\" available" }
```

---

### User Management Endpoints

> Requires `superuser` role. Returns `403` for all other roles.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/users/` | List all users |
| `POST` | `/users/` | Manually create a user by email |
| `PUT` | `/users/<id>` | Update role and/or linked customer |
| `GET` | `/users/customers_list` | List customers (for link dropdown) |

#### List Users

```
GET http://127.0.0.1:5001/users/
```

`200 OK`
```json
[
  {
    "id": 1,
    "email": "kimothomark93@gmail.com",
    "name": "Mark Kimotho",
    "role": "superuser",
    "customer_id": null,
    "customer_name": null,
    "created_at": "2026-03-28T10:00:00"
  }
]
```

---

#### Create a User

Use this when someone cannot log in themselves (e.g. Google account issues). They can log in normally once their Google account matches the email you register here.

```
POST http://127.0.0.1:5001/users/
Content-Type: application/json
```
```json
{ "email": "customer@example.com", "name": "John Doe", "role": "user" }
```

`201 Created`
```json
{ "message": "User created", "user": { "id": 2, "email": "customer@example.com", ... } }
```

`400 Bad Request`
```json
{ "error": "A user with this email already exists" }
```

---

#### Update a User

Change a user's role or link them to a customer profile. Changes take effect on the user's **next login**.

```
PUT http://127.0.0.1:5001/users/2
Content-Type: application/json
```
```json
{ "role": "admin", "customer_id": 3 }
```

`200 OK`
```json
{ "message": "User updated successfully", "user": { ... } }
```

---

### Common Errors

| Status | Meaning |
|---|---|
| `401 Unauthorized` | Not logged in |
| `403 Forbidden` | Logged in but insufficient role for this action |
| `404 Not Found` | Resource does not exist |
| `400 Bad Request` | Missing or invalid request data |
| `409 Conflict` | Idempotency / state conflict (e.g. closing a closed shift) |
| `500 Internal Server Error` | Unexpected server error |

---

### POS Endpoints

> Requires `cashier`, `pharmacist`, `admin`, or `superuser` role.

**Shifts**

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/pos/shift/open` | Open a shift (one per cashier at a time). Body: `{opening_float}` |
| `GET` | `/pos/shift/current` | Current open shift for the logged-in cashier |
| `POST` | `/pos/shift/close` | Close shift. Body: `{closing_cash}`. Returns Z-report |
| `POST` | `/pos/shift/cash_movement` | Record cash in/out. Body: `{movement_type, amount, reason}` |

**Transactions**

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/pos/transaction/new` | Start a new open transaction. Optional `customer_id` |
| `GET` | `/pos/transaction/<id>` | Fetch transaction with items |
| `POST` | `/pos/transaction/<id>/add_item` | Add line. Body: `{inventory_item_id, quantity, unit_price?}` |
| `POST` | `/pos/transaction/<id>/remove_item` | Body: `{transaction_item_id}` |
| `POST` | `/pos/transaction/<id>/apply_discount` | Body: `{amount}` or `{percent}` |
| `POST` | `/pos/transaction/<id>/complete` | Body: `{payment_method, ...}`. Deducts stock, awards loyalty, sends SMS |
| `POST` | `/pos/transaction/<id>/void` | Manager-OTP void. 1st call sends OTP → returns `202`. 2nd call with `{otp}` voids |
| `POST` | `/pos/transaction/<id>/refund` | Body: `{items: [...], reason}`. Partial refunds keep status; full → `refunded` |
| `GET` | `/pos/transaction/<id>/receipt?format=html\|sms\|thermal` | Render receipt in 3 formats |

---

### Payment Endpoints

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/payments/mpesa/stk_push` | cashier+ | Body: `{transaction_id, phone}`. Triggers Safaricom STK |
| `POST` | `/payments/mpesa/callback` | **public** | Daraja callback. Idempotent. Always returns `ResultCode: 0` |
| `GET` | `/payments/mpesa/status/<checkout_request_id>` | cashier+ | Query STK status |
| `POST` | `/payments/card/initiate` | cashier+ | Body: `{transaction_id}`. Returns Flutterwave hosted link |
| `POST` | `/payments/card/webhook` | **public** | Validates `verif-hash` then re-verifies via Flutterwave API |
| `GET` | `/payments/card/verify/<flw_ref>` | cashier+ | Manual verification fallback |

---

### Pharmacy Endpoints

> Requires `pharmacist`, `admin`, or `superuser`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/pharmacy/prescriptions` | Create. Body: `{customer_id, doctor_name, items: [...]}` |
| `GET` | `/pharmacy/prescriptions?customer_id=&open=true` | List with filters |
| `GET` | `/pharmacy/prescriptions/<id>` | Get one with items |
| `POST` | `/pharmacy/prescriptions/<id>/dispense` | Body: `{transaction_id, items: [{prescription_item_id, quantity_dispensed}]}` |
| `GET` | `/pharmacy/controlled_drugs` | Full dispensing ledger per controlled drug |

---

### Reports Endpoints

> Requires `admin` or `superuser`. All accept `?from=&to=` (defaults to last 30 days). Endpoints with `?format=csv` return a downloadable CSV.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/reports/sales_summary` | Total, count, avg basket, by-method breakdown |
| `GET` | `/reports/shift/<id>` | Z-report for one shift |
| `GET` | `/reports/inventory_valuation` | Cost vs retail value, projected margin |
| `GET` | `/reports/stock_movement` | Movement audit |
| `GET` | `/reports/low_stock` | Items at/under reorder level |
| `GET` | `/reports/expiry?days=30` | Expiring soon + cost at risk |
| `GET` | `/reports/top_products?limit=10` | Best sellers |
| `GET` | `/reports/cashier_performance` | Per-cashier sales / refunds / net |

---

## Project Structure

```
make-an-order/
├── app.py                    # create_app() factory, routes, blueprints, CLI
├── config.py                 # Environment-based configuration
├── models.py                 # SQLAlchemy models — legacy + POS extensions
├── requirements.txt
├── Procfile                  # Gunicorn entry point (production)
│
├── api/
│   ├── customers.py          # /customers/* + loyalty / credit endpoints
│   ├── orders.py             # legacy /orders/*
│   ├── inventory.py          # /inventory/* + suppliers / POs / low-stock / expiry
│   ├── users.py              # /users/* — superuser only
│   ├── pos.py                # /pos/* — shifts + transactions (cashier+)
│   ├── payments.py           # /payments/mpesa/* + /payments/card/* (+ webhooks)
│   ├── pharmacy.py           # /pharmacy/* — prescriptions, controlled-drug ledger
│   └── reports.py            # /reports/* — sales, inventory, expiry, performance, CSV
│
├── auth/
│   ├── auth_routes.py        # /login, /authorize, /logout + customer login
│   └── auth_middleware.py    # login/admin/superuser/cashier/pharmacist/stock_clerk
│                             # decorators (UI + *_api_required JSON variants)
│
├── services/
│   ├── database_service.py   # MySQL database creation (flask create-db)
│   ├── seed_service.py       # Sample inventory seeding (run by onboarding)
│   ├── sms_service.py        # Africa's Talking SMS (order, payment, low-stock, etc.)
│   ├── mpesa_service.py      # Daraja STK Push, status, callback parser
│   ├── flutterwave_service.py# Card payment links + webhook validation
│   ├── receipt_service.py    # build / SMS / 42-col thermal receipts
│   ├── loyalty_service.py    # award / redeem / balance
│   ├── audit_service.py      # AuditLog writer (never re-raises)
│   └── otp_service.py        # Manager OTPs — Redis with in-memory fallback
│
├── migrations/               # Alembic: env.py, env_helpers.py, versions/0001_baseline.py
│
├── templates/
│   ├── base.html             # Navbar (role-aware tabs + role badges)
│   ├── pos.html              # Cashier POS surface
│   ├── shift.html            # Shift open / close + Z-report
│   ├── pharmacy.html         # Capture / dispense / controlled-drug ledger
│   ├── purchase_orders.html  # Suppliers + POs + receive flow
│   ├── reports_dashboard.html# KPIs + charts + CSV export
│   ├── inventory.html        # Live shop
│   ├── customers.html, orders.html, users.html, dashboard.html ...
│   └── 404.html
│
├── static/
│   └── style.css             # macOS Platinum POS theme
│
└── tests/
    ├── conftest.py           # app (fresh migrated SQLite per test), client, login_as
    ├── test_app_factory.py   # no I/O at import, ProxyFix, naming convention
    ├── test_migrations.py    # baseline round trip, parity with models, stamping
    ├── test_auth.py
    ├── test_customers.py
    ├── test_models.py
    ├── test_orders.py
    ├── test_pos_models.py
    ├── test_sms_service.py   # lazy Africa's Talking client
    └── test_pos_services.py  # Phone formatting, STK callback parser, OTP, receipts
```

---

## Environment Variables

See [.env.example](.env.example) for the full template. Key variables:

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth 2.0 credentials |
| `APP_SECRET_KEY` | Flask session secret key |
| `MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` | Local MySQL |
| `JAWSDB_URL` | Production MySQL URL (overrides the four above) |
| `AT_USERNAME` / `AT_API_KEY` / `AT_SENDER_ID` | Africa's Talking SMS |
| `MPESA_ENV` | `sandbox` or `production` |
| `MPESA_CONSUMER_KEY` / `MPESA_CONSUMER_SECRET` | Daraja credentials |
| `MPESA_BUSINESS_SHORT_CODE` / `MPESA_PASSKEY` | STK Push config |
| `MPESA_CALLBACK_URL` | Public HTTPS URL for the STK callback |
| `FLW_PUBLIC_KEY` / `FLW_SECRET_KEY` / `FLW_WEBHOOK_SECRET` | Flutterwave |
| `BUSINESS_NAME` | Receipt header |
| `LOYALTY_POINTS_RATIO` | KES spent per 1 loyalty point (default `10`) |
| `TAX_RATE` | VAT rate added on top of subtotal (default `0.16`) |
| `LOW_STOCK_DAYS` | Near-expiry window in days (default `30`) |
| `MANAGER_OTP_TTL` | Manager-override OTP lifetime in seconds (default `300`) |
| `REDIS_URL` | Optional — for OTP storage. Falls back to in-memory if absent |
| `PROXY_FIX_X_FOR` / `PROXY_FIX_X_PROTO` | Reverse-proxy hops to trust (default `1`, Heroku's router; add one per CDN/WAF) |

> `JAWSDB_URL` (set by Heroku) overrides the individual MySQL variables automatically.

---

## Testing

```bash
pytest
```

The suite needs no MySQL, Redis, credentials or `.env`. Each test gets a
fresh SQLite database built by the real migrations (`tests/conftest.py`).
Use the `login_as(role)` fixture for routes behind a role.

Covers:
- `test_app_factory.py` — app builds with no network I/O, endpoint names, ProxyFix hops, naming convention
- `test_migrations.py` — baseline upgrade/downgrade, zero diff against the models, every constraint named, stamping an old database
- `test_auth.py` — login, landing, logout redirects
- `test_customers.py` — customer endpoints and their role checks
- `test_orders.py` — legacy order endpoints
- `test_models.py`, `test_pos_models.py` — ORM models
- `test_sms_service.py` — Africa's Talking client starts lazily and never raises
- `test_pos_services.py` — phone normalisation, M-Pesa callback parser, OTP service, receipt formatting
