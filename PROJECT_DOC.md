---
published: true
layout: page
title: Make-An-Order — Multi-Role POS & Inventory System
description: Point of Sale system with real-time inventory, SMS, analytics, and role-based access
img: assets/img/project_makeorder.jpg
importance: 17
category: projects
---

## Overview

**Make-An-Order** is a POS system for managing orders, inventory, and customers. Built with Flask and MySQL. Handles multiple user roles (superuser, admin, operator), real-time stock tracking, order management, SMS notifications, and analytics dashboards.

The key thing: staff accounts (users) are completely separate from customer accounts. Staff logs in and manages the system. Customers browse and buy. Guests can look at inventory without signing in.

## What It Does

### Core Features

**Order Management**
- Staff places orders on behalf of customers
- Customers place their own orders with a code/email lookup
- Orders go through states: pending → confirmed → completed
- Can also cancel or refund orders
- Automatic SMS sent to customer when order placed

**Inventory**
- Add products with SKU, category, price, stock level
- Stock automatically decreases when orders placed
- Stock increases when you restock
- If an order gets cancelled or refunded, stock goes back
- Log of every stock change: what moved, why, who did it, when

**Customers**
- Create customer records with name, phone, unique code
- Only admins create/edit/delete customers
- Customers place orders tied to their record
- Complete order history per customer

**Users (Staff)**
- Login via Google OAuth
- Three roles: superuser (godmode), admin (run the business), user (operate register)
- Superuser: create/delete users, manage roles, approve pending staff
- Admin: manage customers, inventory, orders, see reports
- User: browse inventory, place orders, see their own history

**Reports**
- Revenue trends over time
- Top selling products
- Payment method breakdown
- Order status distribution
- Custom date range filtering
- Download as CSV

**Analytics Dashboard**
- Chart.js visualizations
- All reports on one page
- Filter by date

## Tech Stack

| What | Technology |
|------|-----------|
| Backend | Flask 3.0 |
| Database | MySQL 5.7+ / MariaDB |
| ORM | SQLAlchemy 2.0 |
| Auth | Google OAuth 2.0 (Authlib) |
| SMS | Africa's Talking API |
| Frontend | Bootstrap 5.3, Jinja2 |
| Charts | Chart.js |
| Session | Flask-Session |
| Production Server | Gunicorn |
| Tests | Pytest |

## How It Actually Works

### User Flows

**Staff Sign In**
1. Click "Staff Member" on role select page
2. Google OAuth login
3. User account created/updated
4. Lands on dashboard

**Customer Sign In**
1. Click "Customer" on role select page
2. Enter customer code or email
3. System finds matching customer record
4. Can browse shop and place orders

**Guest Browsing**
1. Click "Continue as Guest"
2. Lands on public shop
3. Can see inventory but can't place orders

### Actually Placing an Order

**If admin:**
1. Go to inventory page
2. Dropdown at top: select which customer this order is for
3. Add items from inventory (click items, pick quantity)
4. Click "Place Order"
5. Order created with customer linked
6. SMS sent to customer's phone number

**If customer:**
1. Go to shop
2. Add items (no customer dropdown, it's assumed to be them)
3. Click "Place Order"
4. Order linked to their customer record
5. SMS confirmation sent

**If guest:**
1. Can see inventory
2. "Place Order" button is grayed out/hidden
3. Shows message: "Sign in to place orders"

## API Routes

### Auth (`/auth`)
- `GET /auth/login` — Start Google OAuth
- `GET /auth/authorize` — Google sends you back here
- `POST /auth/login/local` — Email/password signin
- `POST /auth/customer-login` — Look up customer by code/email
- `GET /auth/logout` — End session

### Inventory (`/inventory`)
- `GET /api/inventory` — All products
- `POST /api/inventory` — Create product (admin only)
- `PUT /api/inventory/<id>` — Edit product (admin only)
- `DELETE /api/inventory/<id>` — Delete product (superuser only)
- `GET /api/inventory/search?q=term` — Search by name
- `POST /api/inventory/restock` — Add stock (admin only)
- `GET /api/inventory/audit-trail` — See all stock changes

### Orders (`/orders`)
- `POST /api/orders/place_order` — Create order
- `GET /api/orders/view_orders` — Get all orders (admin only)
- `GET /api/orders/<id>` — Get specific order
- `PUT /api/orders/<id>` — Change status (admin only)
- `DELETE /api/orders/<id>` — Delete (admin only)
- `GET /api/orders/<id>/receipt` — Generate receipt
- `GET /api/orders/export/csv` — Download as CSV

### Customers (`/customers`)
- `POST /api/customers/register` — Create (admin only)
- `GET /api/customers` — Get all (admin only)
- `GET /api/customers/<id>` — Get one
- `PUT /api/customers/<id>` — Edit (admin only)
- `DELETE /api/customers/<id>` — Delete (superuser only)

### Users (`/users`)
- `GET /api/users` — Get all (superuser only)
- `PUT /api/users/<id>/role` — Change role (superuser only)
- `PUT /api/users/<id>/customer` — Link to customer (superuser only)
- `DELETE /api/users/<id>` — Delete (superuser only)

### Reports (`/reports`)
- `GET /api/reports/revenue` — Revenue by date
- `GET /api/reports/top-products` — Top sellers
- `GET /api/reports/payment-breakdown` — By payment type
- `GET /api/reports/order-status` — Status distribution

## Database Tables

**users**
- id, email, name, picture_url, role, customer_id, created_at, updated_at

**customers**
- id, code, name, phone_number, email, created_at, updated_at

**inventory**
- id, name, sku, category, description, price, quantity, reorder_level, created_at, updated_at

**orders**
- id, customer_id, total_amount, status, payment_method, payment_reference, created_at, updated_at

**order_items**
- id, order_id, inventory_id, quantity, unit_price, subtotal

**stock_movements**
- id, inventory_id, movement_type, quantity_changed, reason, created_by, created_at

The stock_movements table logs everything: every sale removes units, every restock adds them, every cancel/refund restores them. Tied to the operator who did it so you can audit who changed what.

## Development Setup

### What You Need

- Python 3.8+
- MySQL running locally (or any MySQL service)
- Google OAuth credentials (get from Google Cloud Console)
- Africa's Talking account (optional, SMS just fails gracefully without it)

### Setup Steps

1. Clone the repo
```bash
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order
```

2. Create virtual environment
```bash
python3 -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
```

3. Install dependencies
```bash
pip install -r requirements.txt
```

4. Create .env file
```bash
cp .env.example .env
# Edit .env with your values
```

5. .env needs these
```
APP_URL=http://localhost:5001
APP_SECRET_KEY=something-random

GOOGLE_CLIENT_ID=your-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-secret

MYSQL_HOST=127.0.0.1
MYSQL_USER=root
MYSQL_PASSWORD=your-password
MYSQL_DB=customers_orders

AT_USERNAME=sandbox
AT_API_KEY=your-key
AT_SENDER_ID=your-sender-id
```

6. Important: Add redirect URI to Google Console
   - Go to Google Cloud Console
   - OAuth 2.0 Client IDs (Credentials)
   - Add to "Authorized redirect URIs": `http://localhost:5001/auth/authorize`

7. Initialize database and start
```bash
python3 app.py
```

Go to `http://localhost:5001`

## Deploying to Production

### On Render

1. Push to GitHub
2. Go to Render dashboard
3. Create new Web Service from repo
4. Set these environment variables (same as .env):
   - APP_URL=https://your-render-domain.com
   - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, etc
5. Add database (Render PostgreSQL or link external MySQL)
6. Deploy

Then add to Google Console OAuth:
- `https://your-render-domain.com/auth/authorize`

### On Heroku

1. Install Heroku CLI
2. Add JawsDB MySQL add-on: `heroku addons:create jawsdb:kitefin`
3. Deploy: `git push heroku main`
4. Set config: `heroku config:set APP_URL=https://your-app.herokuapp.com ...`
5. Add to Google Console OAuth: `https://your-app.herokuapp.com/auth/authorize`

## Why Users and Customers Are Separate

This was a rough lesson learned. Originally I tried to make everything one "User" table. Staff and customers all in one model. Auto-created customer records when staff placed orders (USR-1, USR-2, etc).

Then I built analytics showing "top customers" - and it was all staff test accounts. Had to rewrite everything.

The fix: separate tables completely.

**User** = staff account. Login email. Role-based permissions. Manages system.

**Customer** = buyer record. Phone number. Places orders. Gets SMS.

These are completely different things. Staff does not show up in customer analytics. Customer does not need an email. They do not have roles. They just buy things.

## Project Phases

**Phase 1-2:** Basic order and customer CRUD, Google OAuth, SMS notifications

**Phase 3:** Inventory system, multiple categories, stock tracking, search

**Phase 4:** Analytics with Chart.js, revenue trends, top products

**Phase 5:** CSV exports, bulk operations, report downloads

**Phase 6:** Digital receipts, stock audit trail, guest shop, role selection gateway

## Testing

```bash
# Run all tests
pytest tests/

# Run specific test
pytest tests/test_orders.py -v

# With coverage
pytest --cov=api tests/
```

## File Structure

```
make-an-order/
├── api/
│   ├── customers.py      # Customer endpoints
│   ├── inventory.py      # Inventory + orders
│   ├── orders.py         # Order management
│   ├── reports.py        # Analytics
│   └── users.py          # User management
├── auth/
│   └── auth_routes.py    # OAuth + login
├── services/
│   ├── seed_service.py   # DB init
│   └── sms_service.py    # SMS sending
├── templates/
│   ├── landing.html        # Marketing page
│   ├── role_select.html    # Pick staff or customer
│   ├── index.html          # Staff login
│   ├── customer_login.html # Customer lookup
│   ├── base.html           # Main layout
│   ├── inventory.html      # Shop/POS interface
│   ├── customers.html      # Customer mgmt
│   ├── orders.html         # Order mgmt
│   ├── reports.html        # Analytics
│   └── receipts.html       # Order receipts
├── static/
│   └── style.css           # Styling
├── models.py               # Database models
├── config.py               # Settings
├── app.py                  # Flask app
└── requirements.txt        # Dependencies
```

## Important Architecture Details

### Stock Audit Trail

Every inventory change gets logged to `stock_movements`:

```python
StockMovement(
    inventory_id=product.id,
    movement_type='sale',
    quantity_changed=-5,
    reason='order #123',
    created_by=admin_user.id
)
```

This lets you answer: who changed this stock, when, and why. You can trace back any discrepancy.

### Role Checks Happen in Two Places

Frontend hides buttons/fields based on role.

```html
{% if user_role == 'admin' %}
  <select><!-- customer selector --></select>
{% endif %}
```

But backend also validates. API checks role before allowing action. Frontend hiding is UX. Backend check is security.

### OAuth Configuration

The app needs to know its own URL. You pass it in APP_URL environment variable.

```python
app_url = os.getenv("APP_URL", "http://localhost:5001")
redirect_uri = f"{app_url}/auth/authorize"
google = oauth.register(redirect_uri=redirect_uri, ...)
```

This matches what's registered in Google Console. Prevents redirect_uri_mismatch errors when deploying to different environments.

### SMS is Optional

If Africa's Talking is down or the API key is wrong, the order still goes through. SMS is a nice-to-have.

```python
try:
    send_sms(phone, message)
except Exception as e:
    logger.warning(f"SMS failed: {e}")
    # Continue anyway
```

## Known Limitations

- SMS only supports Africa's Talking (no Twilio fallback)
- No built-in payment processing (external reference supported)
- No inventory forecasting yet
- No multi-location/franchise support yet
- UI theme is fixed (no white-label)

## Contributing

Open issues on [GitHub](https://github.com/Markkimotho/make-an-order) or submit PRs. Follow PEP 8. Include tests for new features.

## License

MIT - See LICENSE file.

---

**Updated:** March 31, 2026
**Maintainer:** [@Markkimotho](https://github.com/Markkimotho)
