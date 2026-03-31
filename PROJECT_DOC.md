---
published: true
layout: page
title: Make-An-Order — Multi-Role POS & Inventory System
description: Enterprise-grade Point of Sale system with real-time inventory, SMS notifications, analytics, and role-based access control
img: assets/img/project_makeorder.jpg
importance: 17
category: projects
---

## Overview

**Make-An-Order** is an enterprise-grade Point of Sale (POS) system designed for retail and service businesses. It combines order management, real-time inventory tracking, multi-user role-based access control, customer management, advanced analytics, and SMS integration into a cohesive platform.

Built with Flask, SQLAlchemy, and Bootstrap 5, it provides a modern interface for staff to manage operations while maintaining complete separation between user accounts (staff/operators) and customer records (buyers). Guests can browse and shop without authentication.

## Key Features

### 🎯 Role-Based Access Control (RBAC)

The system enforces three distinct roles with granular permissions:

- **Superuser** — Full platform access, user management, protected account (`kimothomark93@gmail.com`)
- **Admin** — Customer & order management, inventory control, user approvals, analytics
- **User (Operator)** — Browse inventory, place orders, view their order history
- **Customer** — Browse shop, place orders with customer code/email lookup
- **Guest** — Browse inventory without authentication, cannot place orders

### 🛒 Cart-Based Ordering System

- Visual inventory browser with search and filtering
- Add items to cart with quantity adjustments
- Real-time stock validation
- Admin can place orders on behalf of customers
- Automatic order confirmation SMS via Africa's Talking
- Support for multiple payment methods with reference tracking

### 📦 Real-Time Inventory Management

- Live stock tracking with automatic decrement on sales
- Stock audit trail logging every movement with reason (sale, restock, cancel, refund)
- Barcode/SKU scanning support
- Low-stock alerts
- Historical inventory tracking by category
- Restock management with admin dashboard
- CSV export for external analysis

### 👥 Customer Management System

- Customer CRUD operations (admin+)
- Unique customer codes for identification
- Phone number storage for SMS notifications
- Complete order history per customer
- Separate database table from user accounts (fixes architectural misunderstanding)

### 📊 Advanced Analytics & Reporting

- Revenue trends with time-series data
- Top-selling products and categories
- Payment method breakdown
- Order status distribution
- Custom date range filtering
- Chart.js visualizations (bar, pie, line charts)
- CSV export of all reports

### 🧾 Digital Receipts & Exports

- Generate receipts per order with full item details
- Print-friendly receipt interface
- Download receipts as documents
- Bulk export orders to CSV with columns:
  - Order ID, Customer, Phone, Items, Total, Status, Payment Method, Reference, Date
- Export inventory listings
- Export analytics data

### 🔐 Google OAuth 2.0 Integration

- Secure single sign-on via Google
- Auto-user creation on first login
- Session persistence (24 hours)
- Role enforcement
- Role-based landing page (staff vs customer selection)
- Proper redirect_uri configuration for local and production environments

### 📱 SMS Integration

- Africa's Talking API integration
- Automatic order confirmation SMS to customers
- Customizable sender ID
- Graceful fallback if SMS unavailable

### 🏠 Landing Page & Role Selection Gateway

- Professional marketing landing page with feature showcase
- Role selection screen (Staff Member vs Customer)
- Separate login flows:
  - **Staff:** Google OAuth or email/password with admin approval
  - **Customer:** Code/email lookup or guest browsing
  - **Guest:** Direct shop access without authentication

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend Framework | Flask 3.0 |
| ORM | SQLAlchemy 2.0 |
| Database | MySQL (local) / JawsDB (production) |
| Authentication | Google OAuth 2.0 (Authlib) |
| SMS Gateway | Africa's Talking API |
| Frontend | Jinja2, Bootstrap 5.3, Font Awesome 6 |
| Charts | Chart.js |
| Session | Flask-Session (filesystem) |
| Web Server | Gunicorn (production) |
| Testing | Pytest |
| Deployment | Heroku / Render |

## User Flows

### Staff Sign-In Flow
```
Landing Page → Role Select ("Staff Member") → Google OAuth Login
  → User created/updated → Role check → Dashboard
```

### Customer Sign-In Flow
```
Landing Page → Role Select ("Customer") → Code/Email Lookup
  → Customer verified → Shop access
```

### Guest Shop Flow
```
Landing Page → "Continue as Guest" → Public Shop (no auth)
  → Browse only (place order disabled)
```

## Core API Endpoints

### Authentication (`/auth`)
- `GET /auth/login` — Initiate Google OAuth
- `GET /auth/authorize` — OAuth callback
- `POST /auth/login/local` — Email/password login
- `POST /auth/customer-login` — Customer code/email lookup
- `GET /auth/logout` — Clear session

### Inventory (`/inventory`)
- `GET /api/inventory` — Get all products with stock
- `POST /api/inventory` — Create product (admin+)
- `PUT /api/inventory/<id>` — Update product (admin+)
- `DELETE /api/inventory/<id>` — Delete product (superuser+)
- `GET /api/inventory/search` — Search products
- `POST /api/inventory/restock` — Add stock (admin+)
- `GET /api/inventory/audit-trail` — View stock movements

### Orders (`/orders`)
- `POST /api/orders/place_order` — Create order
- `GET /api/orders/view_orders` — Get all orders (admin+)
- `GET /api/orders/view_orders/<id>` — Get customer orders
- `PUT /api/orders/<id>` — Update order status (admin+)
- `DELETE /api/orders/<id>` — Delete order (admin+)
- `POST /api/orders/<id>/receipt` — Generate receipt
- `GET /api/orders/export/csv` — Export to CSV

### Customers (`/customers`)
- `POST /api/customers/register` — Create customer (admin+)
- `GET /api/customers/view_customers` — Get all customers (admin+)
- `GET /api/customers/view_customers/<id>` — Get customer details
- `PUT /api/customers/<id>` — Update customer (admin+)
- `DELETE /api/customers/<id>` — Delete customer (superuser+)

### Users (`/users`)
- `GET /api/users/view_users` — Get all users (superuser+)
- `PUT /api/users/<id>/role` — Change user role (superuser+)
- `PUT /api/users/<id>/customer` — Link user to customer (superuser+)
- `DELETE /api/users/<id>` — Delete user (superuser+)

### Reports (`/reports`)
- `GET /api/reports/revenue` — Revenue trends
- `GET /api/reports/top-products` — Top selling products
- `GET /api/reports/payment-breakdown` — Payment method analysis
- `GET /api/reports/order-status` — Order distribution

## Database Schema

### Core Tables

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

**stock_movements** (audit trail)
- id, inventory_id, movement_type, quantity_changed, reason, created_by, created_at

## Features by Phase

**Phase 1-2:** Core order & customer management, Google OAuth, SMS notifications

**Phase 3:** Inventory system with stock tracking, multiple categories, search

**Phase 4:** Advanced analytics with Chart.js, revenue trends, product insights

**Phase 5:** CSV exports for orders, inventory, and reports; bulk operations

**Phase 6:** Digital receipts, audit trails, role-based UI, guest shop support

## Getting Started

### Prerequisites

- Python 3.8+
- MySQL 5.7+ or MariaDB
- Google OAuth 2.0 credentials
- Africa's Talking account (optional)

### Installation

```bash
# Clone repository
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # macOS/Linux: or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Configure .env
cp .env.example .env
# Edit .env with your credentials:
# - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
# - APP_URL (http://localhost:5001 for local dev)
# - MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
# - AT_USERNAME, AT_API_KEY, AT_SENDER_ID

# Initialize database
python3 -c "from app import app, db; db.create_all()"

# Start the app
python3 app.py
```

Access the app at `http://localhost:5001`

### Environment Variables

```env
# Application
APP_URL=http://localhost:5001
APP_SECRET_KEY=your-secret-key

# Google OAuth
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-secret
# NOTE: Add APP_URL/auth/authorize to Google Console Authorized redirect URIs

# MySQL Database
MYSQL_HOST=127.0.0.1
MYSQL_USER=root
MYSQL_PASSWORD=your-password
MYSQL_DB=customers_orders

# Africa's Talking SMS
AT_USERNAME=sandbox
AT_API_KEY=your-api-key
AT_SENDER_ID=your-sender-id
```

## Deployment

### Deploy to Render

```bash
# Create .gitignore for .env
echo ".env" >> .gitignore

# Push to GitHub
git push origin main

# On Render dashboard:
# 1. Create new Web Service
# 2. Connect GitHub repository
# 3. Set environment variables (same as .env)
# 4. Deploy
```

### Deploy to Heroku

```bash
# Add JawsDB MySQL add-on
heroku addons:create jawsdb:kitefin -a your-app-name

# Deploy
git push heroku main

# Set environment variables
heroku config:set GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... -a your-app-name
# Update APP_URL to your Heroku domain
```

## Testing

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_orders.py -v

# Run with coverage
pytest --cov=api tests/
```

## Project Structure

```
make-an-order/
├── api/
│   ├── customers.py      # Customer CRUD endpoints
│   ├── inventory.py      # Inventory & order endpoints
│   ├── orders.py         # Order management
│   ├── reports.py        # Analytics endpoints
│   └── users.py          # User management (superuser)
├── auth/
│   └── auth_routes.py    # Google OAuth & session management
├── services/
│   ├── seed_service.py   # Database initialization
│   └── sms_service.py    # Africa's Talking SMS
├── templates/
│   ├── landing.html      # Marketing landing page
│   ├── role_select.html  # Role selection gateway
│   ├── index.html        # Staff login form
│   ├── customer_login.html # Customer login form
│   ├── register.html     # Staff registration request
│   ├── base.html         # Base layout with conditional nav
│   ├── dashboard.html    # Role-specific dashboard
│   ├── inventory.html    # Shop/POS interface
│   ├── customers.html    # Customer management (admin+)
│   ├── orders.html       # Order management (admin+)
│   ├── reports.html      # Analytics dashboard (admin+)
│   ├── users.html        # User management (superuser+)
│   └── receipts.html     # Order receipts
├── static/
│   ├── style.css         # Bootstrap overrides & custom styles
│   └── icons/            # Font Awesome icons
├── models.py             # SQLAlchemy ORM models
├── config.py             # Configuration & settings
├── app.py                # Flask app initialization & routes
├── requirements.txt      # Python dependencies
└── README.md             # Quick start guide
```

## Architecture Highlights

### User vs Customer Separation

A critical architectural decision: **Users are staff members; Customers are buyers.**

- **User:** Staff account with email login via Google OAuth, role-based access, manages the platform
- **Customer:** Buyer record with phone number, purchases orders, receives SMS notifications

This separation prevents the misunderstanding where staff inadvertently become customers and see their own orders in the customer list.

### Role-Based UI

The same template (`inventory.html`) serves three different user types with conditional logic:

```html
{% if user_role == 'admin' %}
  <!-- Show customer selector, place order button -->
{% elif user_role == 'operator' %}
  <!-- Show place order button, no customer selector -->
{% else %}
  <!-- Guest: show only browse, disable place order -->
{% endif %}
```

### Stock Audit Trail

Every inventory change is logged with:
- Operator who made the change
- Quantity changed
- Reason (sale, restock, cancel, refund)
- Timestamp

This provides complete traceability for audits and debugging.

### OAuth Redirect URI Configuration

The app explicitly configures the OAuth redirect_uri to match Google Console registration:

```python
app_url = os.getenv("APP_URL", "http://localhost:5001")
redirect_uri = f"{app_url}/auth/authorize"
google = oauth.register(
    redirect_uri=redirect_uri,
    ...
)
```

This prevents `redirect_uri_mismatch` errors when deploying to different environments.

## Performance Considerations

- **Inventory Search:** Full-text MySQL INDEX on product names
- **Order Pagination:** 50 items per page with offset-based pagination
- **Stock Audit Trail:** Indexed on inventory_id for fast lookups
- **Session Caching:** 24-hour Flask session persistence
- **SMS Fallback:** Non-blocking, graceful failure if Africa's Talking is unavailable

## Security

- CSRF protection via Flask-WTF (implicit in form submission)
- SQL injection prevention via SQLAlchemy parameterized queries
- XSS protection via Jinja2 auto-escaping
- Role-based access control (RBAC) enforced at API layer
- Session timeout after 24 hours of inactivity
- Superuser account hardcoded and protected from deletion

## Known Limitations & Future Enhancements

- SMS notifications only supported via Africa's Talking (no Twilio fallback yet)
- No built-in payment gateway integration (external reference supported)
- No inventory forecasting based on trends
- No customer loyalty points system
- UI customization limited to CSS (no white-label support yet)

## Support & Contribution

The project is open source. For bugs, feature requests, or contributions:

1. Open an issue on [GitHub](https://github.com/Markkimotho/make-an-order)
2. Fork the repo and submit a pull request
3. Follow PEP 8 style guidelines
4. Include tests for new features

## License

MIT License — See LICENSE file in repository.

---

**Last Updated:** March 31, 2026
**Maintainer:** [@Markkimotho](https://github.com/Markkimotho)
