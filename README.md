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
  - [User Management Endpoints](#user-management-endpoints)
  - [Common Errors](#common-errors)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [Testing](#testing)

---

## Overview

**make-an-order** is a Flask-based RESTful service for managing customers, orders, and inventory. It uses Google OAuth 2.0 for authentication and enforces role-based access so that admins manage everything while regular users only interact with their own tab.

**Features:**
- Google OAuth 2.0 login
- RBAC with three roles: `superuser`, `admin`, `user`
- Customer and order management (admin+)
- Live POS-style inventory shop (all users)
- Automatic SMS notifications on order placement via Africa's Talking
- Superuser panel to manage users, promote roles, and link accounts to customer profiles

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Flask 3.0, SQLAlchemy 2.0 |
| Database | MySQL (local) / JawsDB (production) |
| Auth | Google OAuth 2.0 (Authlib) |
| SMS | Africa's Talking API |
| Frontend | Jinja2, Bootstrap 5.3, Font Awesome 6 |
| Server | Gunicorn (production) |
| Testing | Pytest |

---

## Roles & Access

| Feature | `user` | `admin` | `superuser` |
|---|:---:|:---:|:---:|
| View own orders | ✓ | ✓ | ✓ |
| Browse inventory / place orders on tab | ✓ | ✓ | ✓ |
| Manage customers (CRUD) | ✗ | ✓ | ✓ |
| Manage all orders (CRUD) | ✗ | ✓ | ✓ |
| User management (roles, customer linking) | ✗ | ✗ | ✓ |
| Role badge in UI | none | Admin (blue) | Super (purple) |

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

### Running the App

```bash
python app.py
```

The app starts at **`http://127.0.0.1:5001`**.

On first run it automatically:
- Creates all database tables (`users`, `customers`, `orders`, `inventory_items`)
- Seeds the inventory with 20 mock items across 4 categories (Electronics, Office, Food & Beverage, Accessories)

No migrations are needed — the app manages its own schema via `db.create_all()`.

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
| `500 Internal Server Error` | Unexpected server error |

---

## Project Structure

```
make-an-order/
├── app.py                    # App factory, routes, blueprints
├── config.py                 # Environment-based configuration
├── models.py                 # SQLAlchemy models: User, InventoryItem, Customer, Order
├── requirements.txt
├── Procfile                  # Gunicorn entry point (production)
│
├── api/
│   ├── customers.py          # /customers/* — admin+ only
│   ├── orders.py             # /orders/*   — admin+ (my_orders open to all)
│   ├── inventory.py          # /inventory/* — all users
│   └── users.py              # /users/*    — superuser only
│
├── auth/
│   ├── auth_routes.py        # /login, /authorize, /logout + User record sync
│   └── auth_middleware.py    # login_required, admin_required, superuser_required,
│                             # admin_api_required, superuser_api_required
│
├── services/
│   ├── database_service.py   # MySQL database creation helper
│   ├── seed_service.py       # Mock inventory seeding (runs once on startup)
│   └── sms_service.py        # Africa's Talking SMS integration
│
├── templates/
│   ├── base.html             # Navbar (role-aware tabs + role badges)
│   ├── index.html            # Login page
│   ├── dashboard.html        # Admin: 3-card overview | User: My Account + orders
│   ├── customers.html        # Customer management (admin+)
│   ├── orders.html           # Order management (admin+)
│   ├── inventory.html        # POS shop — all users (role-aware cart)
│   ├── users.html            # User management (superuser only)
│   ├── customer_detail.html
│   ├── customer_edit.html
│   ├── order_edit.html
│   └── 404.html
│
├── static/
│   └── style.css             # macOS Platinum POS theme
│
└── tests/
    ├── test_auth.py
    ├── test_customers.py
    ├── test_models.py
    └── test_orders.py
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | Google OAuth 2.0 client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth 2.0 client secret |
| `APP_SECRET_KEY` | Flask session secret key |
| `MYSQL_HOST` | MySQL host (e.g. `localhost`) |
| `MYSQL_USER` | MySQL username |
| `MYSQL_PASSWORD` | MySQL password |
| `MYSQL_DB` | MySQL database name |
| `AT_USERNAME` | Africa's Talking username (`sandbox` for testing) |
| `AT_API_KEY` | Africa's Talking API key |
| `AT_SENDER_ID` | Africa's Talking sender ID |

> In production, `JAWSDB_URL` (set by Heroku) overrides the individual MySQL variables automatically.

---

## Testing

```bash
pytest tests/
```

Covers customer endpoints, order endpoints, auth routes, and models.
