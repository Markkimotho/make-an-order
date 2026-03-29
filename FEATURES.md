# Make-An-Order: Complete Feature Documentation

**Version:** 1.0
**Last Updated:** 2026-03-30
**Description:** Enterprise-grade POS (Point of Sale) + Inventory Management System with multi-user RBAC, SMS integration, and comprehensive analytics.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [User Roles & Permissions](#user-roles--permissions)
3. [Core Features](#core-features)
   - [Authentication & Authorization](#authentication--authorization)
   - [Order Management](#order-management)
   - [Inventory Management](#inventory-management)
   - [Customer Management](#customer-management)
   - [User Management](#user-management)
   - [Reporting & Analytics](#reporting--analytics)
   - [Receipts & Exports](#receipts--exports)
4. [UI Features](#ui-features)
5. [API Endpoints](#api-endpoints)
6. [Database Schema](#database-schema)
7. [Integrations](#integrations)
8. [Configuration](#configuration)
9. [Deployment Notes](#deployment-notes)

---

## System Overview

**Make-An-Order** is a comprehensive Point of Sale system designed for retail and service businesses. It provides real-time inventory tracking, multi-user support with role-based access control, customer management, sales analytics, and SMS integration for order confirmations.

### Key Capabilities

- 🛒 **Cart-Based Ordering** — Browse inventory, build carts, place orders with stock validation
- 📦 **Real-Time Inventory** — Automatic stock decrement on sales, restock management, low-stock alerts
- 👥 **Multi-User RBAC** — Three role levels with permission-based feature access
- 📊 **Advanced Analytics** — Revenue trends, top sellers, payment breakdowns, custom reports
- 🧾 **Digital Receipts** — Printable/downloadable order receipts with full details
- 📱 **SMS Integration** — Automated order confirmations via Africa's Talking
- 🔍 **Search & Scan** — Text search and barcode/SKU scanning for fast item lookup
- 💾 **CSV Export** — Export orders, inventory, and reports for external analysis
- 🎯 **Google SSO** — Single sign-on via Google OAuth 2.0
- ⚠️ **Stock Audit Trail** — Complete history of every inventory movement with reason tracking

---

## User Roles & Permissions

### 1. **Superuser**
- **Email:** `kimothomark93@gmail.com` (hardcoded, protected from modification/deletion)
- **Permissions:**
  - Full access to all features
  - Create, edit, delete customers
  - Create, edit, delete inventory items
  - Create, approve, reject users
  - Delete orders and users
  - View all reports and analytics
  - Cannot be deleted or role-changed

### 2. **Admin**
- **Permissions:**
  - Create, edit, view customers
  - Create, edit, restock, view inventory
  - Create orders on behalf of customers
  - View and manage orders (status, payment details)
  - Approve/reject user registration requests
  - View all reports and analytics
  - **Cannot:** Delete customers, delete inventory items, delete users

### 3. **User** (Normal/Standard)
- **Permissions:**
  - Browse and place orders from inventory
  - View their own order history and receipts
  - View their profile
  - **Auto-provisioned:** Linked to a `Customer` record with code `USR-{user_id}` on first order
  - **Cannot:** Access management features, admin dashboard, reporting

---

## Core Features

### Authentication & Authorization

#### Google OAuth 2.0 Integration
- **Flow:** Login → Google auth → User created/updated → Session established
- **Profile Data:** Email, name, picture URL
- **Session Duration:** 24 hours (persistent cookie)
- **Protected Routes:** All pages except `/` and `/login` require login
- **Role-Based Routes:**
  - `/dashboard` — all users (different content per role)
  - `/customers-ui` — admin+ only
  - `/orders-ui` — admin+ only
  - `/inventory-ui` — all logged-in users (different UI per role)
  - `/users-ui` — admin+ only
  - `/reports-ui` — admin+ only

#### Session Management
- **Session Variable:** `session['profile']` (user email, name, picture)
- **Role Storage:** `session['role']` (superuser, admin, user)
- **User ID:** `session['user_id']`
- **Logout:** Clears session and redirects to login

#### Authorization Decorators (Backend)
- `@login_required` — Any authenticated user
- `@admin_required` — Admin or superuser (page route)
- `@admin_api_required` — Admin or superuser (API endpoint)
- `@superuser_api_required` — Superuser only
- API endpoints return `401 Unauthorized` or `403 Forbidden` on permission denial

---

### Order Management

#### Order Creation Flow

**Admin Creates Order on Behalf of Customer:**
1. Navigate to Inventory page
2. Select customer from dropdown
3. Add items to cart (click item cards to add qty)
4. Review total and items
5. Click "Place Order"
6. Order created with status `pending`, SMS sent to customer

**Normal User Places Order:**
1. Navigate to Inventory page
2. Add items to cart
3. Click "Place Order"
4. Order auto-linked to user's auto-provisioned customer
5. SMS confirmation sent to user's phone number

#### Order Statuses
- **pending** — Default status on creation
- **confirmed** — Admin confirms readiness to fulfill
- **completed** — Order fulfilled and delivered
- **cancelled** — Order cancelled, stock restored with reason "cancel"
- **refunded** — Order refunded, stock restored with reason "refund"

#### Order Lifecycle Management

**View Orders:**
- Admin: Access `/orders-ui` to see all orders
- Pagination: 50 per page by default
- Filter by customer: Dropdown selector auto-updates table
- Search: Filter box in header (future feature stub)
- Export: "Export CSV" button in card header
- Download: CSV with columns: Order ID, Customer, Phone, Items, Total, Status, Payment Method, Reference, Date

**Update Order Status:**
- Admin can change status via dropdown in table
- Status change triggers stock restoration if moving to cancelled/refunded
- Success/error toast notification
- Page auto-refreshes with updated orders

**Update Payment Details:**
- Admin clicks edit (pencil icon) for full edit modal
- Fields: Item summary, amount, payment method (cash/mpesa/card), payment reference
- Payment reference stored (e.g., M-Pesa transaction code) for audit

**Delete Order:**
- Admin confirms deletion with modal
- Stock is fully restored to inventory with reason "cancel"
- Order removed from database
- Success toast confirms

**View Receipt:**
- Any logged-in user can view receipt for their own orders
- Admin can view any order's receipt
- URL: `/orders/<order_id>/receipt`
- Print-optimized CSS: `@media print` hides navbar and action buttons
- Button in orders table or dashboard

#### Order Items (Line-Level Detail)

**Structure:**
- Each `OrderItem` links to specific `InventoryItem` (if created via inventory flow)
- Captures: item name, quantity, unit price, subtotal
- Enables analytics: top sellers, revenue per item

**Legacy Support:**
- Orders created via free-text flow stored as single string in `Order.item`
- System intelligently switches between `OrderItem` table (new) and parsed string (legacy) for stock restoration

---

### Inventory Management

#### Item CRUD Operations

**Create Inventory Item:**
- Admin/superuser accesses Inventory → Manage Inventory tab
- Form fields: Name, Category, Price, Stock, SKU (optional), Description, Icon (FontAwesome class)
- Initial stock creates `StockMovement` record with reason "initial_stock"
- Success: Item added to shop view immediately

**Read/View Items:**
- All logged-in users see shop view
- Admin sees two tabs: "Shop & Order" + "Manage Inventory"
- Manage tab shows table: Name, Category, Price, Stock (with color badge), Actions
- Low-stock badge on Manage tab header (triggers refresh)

**Update Item:**
- Admin clicks "Edit" button in Manage tab
- Modal opens with current values
- Fields: Name, Category, Price, Description, Icon, SKU
- **Note:** Stock NOT editable here (use Restock button)
- Saves via `PUT /inventory/items/<id>`

**Delete Item:**
- Superuser only (button hidden for admins)
- Confirmation required
- Item and all related `StockMovements` are deleted
- Deleted item removed from shop and manage views

**Restock Item:**
- Admin clicks "Restock" button in Manage tab
- Modal opens: quantity field + optional note
- Creates `StockMovement` record with reason "restock"
- Note field auto-fills with optional restock reason (e.g., "supplier delivery")
- Stock immediately updated

#### Stock Management & Audit

**Stock Decrement on Sale:**
- When order placed, each `OrderItem` decrements corresponding `InventoryItem.stock`
- `StockMovement` created with:
  - `delta`: negative (e.g., -3 for qty ordered)
  - `reason`: "sale"
  - `user_id`: who placed order
  - `note`: order number

**Stock Restoration on Cancel/Refund:**
- When order status moves to `cancelled` or `refunded`:
  - Stock restored (delta positive)
  - `StockMovement` created with reason "cancel" or "refund"
  - Handles both new `OrderItem` records (linked) and legacy item strings (parsed)

**Stock Movement Audit Log:**
- Endpoint: `/inventory/movements` (admin+ only)
- Fields logged: Item ID, item name (snapshot), delta, reason, user ID, note, timestamp
- Query params: `item_id` (filter by item), `limit` (default 50, max 200)
- Reasons: sale, restock, cancel, refund, initial_stock, manual
- Use case: Track every unit of stock movement for accountability

#### Low-Stock Alerts

**Threshold:** 5 units (configurable via `LOW_STOCK_THRESHOLD`)

**Dashboard Alert (Admin Only):**
- Yellow card on admin dashboard with list of low-stock items
- Shows: item name, icon, current stock with "out" or "N" badge
- Clickable link to Inventory page for quick restock
- Updates on page load

**Manage Tab Badge:**
- Red badge shows count: "N low" (e.g., "3 low")
- Updates when switching to Manage tab

**Endpoint:** `GET /inventory/low_stock`
- Returns: threshold, count, item list with details

#### Search & Discovery

**Text Search (Shop View):**
- Search box filters items by name in real-time
- Case-insensitive, partial matching
- Works independently of category filter
- Both filters can be active simultaneously

**Category Filter (Shop View):**
- Buttons for each category (All, Electronics, Office, Food & Beverage, Accessories)
- Click to filter items
- Active category highlighted
- Combines with text search

**Barcode/SKU Scanner (Shop View):**
- Dedicated input field: "Scan or type SKU"
- Press Enter to lookup
- If found and in stock: auto-adds single item to cart
- If out of stock: toast error
- If not found: toast error
- Lookup endpoint: `GET /inventory/items/by-sku/<sku>`

#### Inventory Display

**Item Cards (Shop View):**
- Icon chip with category-based color
- Item name, description, price
- Stock badge: green (>10 left), yellow (5-10 left), red (out)
- "Add to Order" button (disabled if out of stock)
- Click to increment qty in cart

**Item Icons:**
- FontAwesome classes stored in database
- Customizable per item
- Colors by category: Electronics (purple), Office (blue), Food (orange), Accessories (green)

---

### Customer Management

#### Customer CRUD Operations

**Create Customer:**
- Admin accesses `/customers-ui`
- Form fields: Name, Email (optional), Phone Number, Customer Code (unique)
- Phone stored with country code (e.g., +2547XXXXXXXX)
- Code used for filtering orders
- Success: Customer added to table

**Read/View Customers:**
- Admin sees paginated list (50 per page)
- Table: ID, Name, Email, Phone, Code, Actions
- Search by filtering is not yet implemented (stub for future)

**Update Customer:**
- Click "Edit" button in customer row
- Navigates to `/customers/<id>/edit`
- Modal form with all editable fields
- Validation: phone and code must be unique across database

**Delete Customer:**
- Superuser only (button hidden for admins)
- Confirmation required
- Cascades delete: all related orders deleted

**View Customer Detail Page:**
- Click "Details" button in customer row
- Route: `/customers/<id>`
- Shows:
  - Customer info card (ID, name, phone, email, code)
  - Four stat cards: Total Orders, Total Spent, Completed Orders, Pending Orders
  - Order history table with dates, items, amounts, statuses, receipt link
- All stats calculated from customer's orders

#### Auto-Provisioned Customers (User Accounts)

**Trigger:** First time a normal user places an order

**Process:**
1. System checks if user has `phone_number` (required at login or can be added later)
2. Creates `Customer` record if not exists
3. Uses code: `USR-{user_id}` (e.g., `USR-42`)
4. On future orders, same customer object reused
5. User can view order history on their dashboard

**Use Case:** Normal users automatically linked to orders without manual setup

---

### User Management

#### User Registration & Approval

**User Registration Flow:**
1. User clicks "Sign up" on login page → redirects to registration form
2. Fills: Email (auto-filled from Google), Name, Phone Number, password (optional)
3. Submits → User created with role "user" and status "pending"
4. Admin notified (workflow TBD)

**Admin Approval:**
- Admin accesses `/users-ui`
- Sees two sections:
  - **Pending Users:** Status "pending" (yellow badge)
  - **Approved Users:** Role shown (blue for admin, green for user)
- Actions:
  - **Approve:** Changes status "pending" → "approved" (user can now login)
  - **Reject:** Changes status "pending" → "rejected" (user blocked)
  - **Promote to Admin:** Changes role "user" → "admin"
  - **Demote to User:** Changes role "admin" → "user"
  - **Delete:** Removes user entirely (not superuser)
  - **Pending Count Badge:** Red badge on nav "Users/Approvals" link shows count

#### User Fields

- **Email** (unique) — Primary identifier, from Google OAuth
- **Name** — Display name
- **Phone Number** — For orders and SMS
- **Role** — superuser, admin, user
- **Status** — pending, approved, rejected
- **Password Hash** — Optional (null for OAuth-only users)
- **Created At** — Timestamp

#### Permission Changes

**Admin Permissions:**
- Cannot change their own role to superuser
- Cannot change superuser's role/email/delete superuser
- Can approve/reject registrations
- Can promote/demote between user and admin

**Superuser Permissions:**
- Can do everything admins can do PLUS:
  - Delete any user or admin
  - Change any user to admin or back
  - No restrictions except cannot self-delete or change own email

---

### Reporting & Analytics

#### Dashboard Overview (Admin Only)

**Four Main Cards:**
1. **Customers** — Total customer records with link to Customers page
2. **Orders** — Total orders with link to Orders page
3. **Inventory** — Live product catalog with shop link
4. **Users/Approvals** — Pending approvals count (badge updates in real-time)

**Low Stock Alert** (contextual):
- Shows if < 5 items low stock
- Displays item list with current stock
- Link to Inventory Manage tab

**Pending User Count Badge:**
- Red badge on "Users/Approvals" card
- Also appears in nav bar across all pages

#### Sales Report Page

**URL:** `/reports-ui` (admin+ only)

**Period Selector:**
- Buttons: Today, 7 Days, 30 Days (default), All Time
- Clicking refreshes all data and updates export link

**Summary Stat Cards:**
- **Total Revenue** — Sum of all orders in period (pending, confirmed, completed only; excludes cancelled/refunded)
- **Orders** — Count of orders in period
- **Avg Order Value** — Revenue ÷ order count

**Revenue Line Chart:**
- X-axis: Date range (auto-fills missing days with $0)
- Y-axis: Revenue
- Shows daily revenue trend
- Helpful for identifying peak sales days

**Top Items by Revenue Table:**
- Columns: Rank, Item Name, Units Sold, Revenue, % of Total Revenue
- Shows which products drive most sales
- Top 10 by default

**Order Status Breakdown Doughnut Chart:**
- Segments: pending (yellow), confirmed (blue), completed (green), cancelled (red), refunded (gray)
- Custom legend below chart
- Shows order distribution by state

**Payment Methods Table:**
- Columns: Method, Order Count, Revenue, % of Total Revenue
- Shows cash vs M-Pesa vs card breakdown
- Identifies primary payment channels

**CSV Export:**
- Button in header: "Export CSV"
- Downloads: revenue_report.csv
- Contains: Date, Order Count, Revenue for revenue-by-day data
- Filename auto-sets based on period

#### Report Data & Calculations

**Revenue Calculation:**
- Includes statuses: pending, confirmed, completed
- Excludes: cancelled, refunded
- Formula: `sum(Order.amount)` where status in above set

**Time Filtering:**
- Today: 00:00 to 23:59 UTC
- 7 Days: last 7 24-hour periods
- 30 Days: last 30 24-hour periods
- All Time: all orders ever

**API Endpoints:**
- `GET /reports/summary?period={today|week|month|all}` — Revenue KPIs
- `GET /reports/by_day?days={N}` — Daily revenue with gap-filling
- `GET /reports/top_items?limit={N}` — Top sellers
- `GET /reports/status_breakdown` — Order status distribution
- `GET /reports/payment_breakdown` — Payment method breakdown
- `GET /reports/export?days={N}` — CSV export

---

### Receipts & Exports

#### Printable Receipt

**URL:** `/orders/<order_id>/receipt` (any logged-in user for own orders, admin for all)

**Receipt Content:**
- Business name/header
- Order number and date
- Customer: Name, Phone, Email, Customer Code
- Line items: Item name, quantity, unit price, subtotal
- **Order Total** (bold, large)
- Payment method and reference (if recorded)
- Footer: "Thank you for your order" message

**Print Optimization:**
- CSS: `@media print` hides navbar and action buttons
- Layout: centered, clean, thermal-printer friendly
- Button: "Print" opens browser print dialog (Ctrl+P or Cmd+P)
- Reflow: responsive CSS adapts to page width

**Styling:**
- Uses site variables: `--mac-text`, `--mac-border-lt`, etc.
- Bootstrap utilities for alignment
- Custom print styles for receipt-specific formatting

#### CSV Exports

**Orders Export:**
- Endpoint: `GET /orders/export?customer_id={id}&status={status}&period={period}`
- Filename: `orders.csv`
- Columns: Order ID, Customer, Phone, Items, Total, Status, Payment Method, Payment Reference, Date
- Filtering: By customer, status, time period
- Button location: Orders page (card header)

**Inventory Export:**
- Endpoint: `GET /inventory/export?category={category}`
- Filename: `inventory.csv`
- Columns: ID, Name, Category, Price, Stock, Description
- Filtering: By category (optional)
- Use case: Stock reconciliation, purchasing decisions

**Reports Export:**
- Endpoint: `GET /reports/export?days={N}`
- Filename: `revenue_report.csv`
- Columns: Date, Order Count, Revenue
- Time range: Last N days (max 365)
- Button location: Reports page (header)

#### Export Workflow

1. User navigates to page with export button
2. Applies filters (customer, status, category, period) as desired
3. Clicks "Export CSV" button
4. Browser downloads CSV file with timestamp/context in filename
5. CSV opens in Excel, Google Sheets, or other spreadsheet app
6. User can further analyze or share data

---

## UI Features

### Navigation

**Top Navigation Bar:**
- Logo/brand name (left)
- Nav links (center): Customers, Orders, Inventory, Reports (admin+), Users/Approvals (admin+)
- User profile menu (right): Display name, "Sign out" option
- **Pending Badge:** Red badge on Users/Approvals link shows count

**Responsive Design:**
- Mobile-friendly Bootstrap 5 layout
- Hamburger menu on small screens
- Touch-friendly button sizes

### Dashboard (Per-Role)

**Admin Dashboard:**
- Four main cards: Customers, Orders, Inventory, Users
- Low-stock alert (if applicable)
- All cards link to respective management pages

**User Dashboard:**
- Profile card: Name, Email, Total Orders, Total Spent
- My Orders table: Order #, Item, Amount, Status, Date, Receipt link
- Receipt link in each row for quick access

### Forms & Modals

**Add/Edit Modals:**
- Form validation: required fields, min/max constraints
- Submit button on modal footer
- Cancel/close button to dismiss
- Toast notifications on success/error

**Toast Notifications:**
- Fixed position: top-right corner
- Green background: success
- Red background: error
- Auto-dismiss: 5 seconds
- Message: action result (e.g., "Order updated successfully")

### Tables

**Pagination:**
- Default: 50 rows per page
- Configurable query param: `?per_page=100`
- Shows: current page, total pages, total records
- Navigation: Previous/Next or numbered buttons (TBD)

**Sorting:**
- Click column header to sort (most tables)
- Ascending/descending toggle
- Reset to default order

**Filtering:**
- Dropdown or text-based filters
- Real-time updates or "Apply Filter" button
- Multi-criteria support (e.g., customer + status)

**Responsive Tables:**
- Horizontal scroll on mobile
- Collapsible columns on very small screens
- Sticky headers for long scrolling

### Charts (Chart.js 4.4.3)

**Line Chart (Revenue):**
- Shows revenue trend over time
- Tooltips on hover: date, revenue amount
- Gap-filling: missing days rendered as $0
- Smooth line with fill underneath

**Doughnut Chart (Order Status):**
- Segments with category colors
- Custom legend showing count per status
- Click legend item to toggle segment visibility

**Progress Bars:**
- Top items: share percentage as horizontal bar
- Payment methods: revenue share as horizontal bar
- Color-coded: green for revenue, blue for payment methods

### Badges & Indicators

**Stock Badges:**
- Green: >= 11 units in stock
- Yellow: 5-10 units in stock
- Red: 0 units in stock
- Text: quantity or "out of stock"

**Status Badges (Orders):**
- Yellow: pending
- Blue: confirmed
- Green: completed
- Red: cancelled
- Gray: refunded

**Role & Permission Indicators:**
- Badge next to user name: "Admin", "Superuser", "User"
- Color-coded for quick visual identification

### Search & Filter UI

**Search Boxes:**
- Inventory: Search by item name (real-time filter)
- Category: Buttons to filter by category
- Barcode Scanner: Press Enter after typing/scanning

**Filter Dropdowns:**
- Orders: Filter by customer
- Customers: Filter by status (approved/rejected/pending)
- Users: Filter by role

---

## API Endpoints

### Authentication Endpoints

```
POST /auth/logout
  → Clears session, redirects to login

POST /auth/google-callback
  → OAuth callback, creates/updates user, sets session
  → Redirects: superuser/admin → /dashboard, user → /dashboard
```

### Order Endpoints

```
GET /orders/view_orders
  → List all orders (paginated)
  → Query params: page=1, per_page=50
  → Response: { orders: [...], pagination: {...} }
  → Auth: admin+

GET /orders/view_orders/<customer_id>
  → List orders for specific customer (paginated)
  → Auth: admin+

GET /orders/my_orders
  → List current user's orders
  → Auth: any logged-in user
  → Response: Array of order objects

POST /orders/place_order
  → Legacy: Create manual order (deprecated, use /inventory/place_order)
  → Auth: admin+

PUT /orders/<id>/status
  → Update order status (triggers stock restoration if cancelled/refunded)
  → Body: { status: "completed" }
  → Auth: admin+

PUT /orders/<id>/payment
  → Update payment method & reference
  → Body: { payment_method: "mpesa", payment_reference: "..." }
  → Auth: admin+

PUT /orders/update_orders/<id>
  → Update order item summary and amount
  → Body: { item: "...", amount: 999.99 }
  → Auth: admin+

DELETE /orders/delete_orders/<id>
  → Delete order and restore stock
  → Auth: admin+

GET /orders/export
  → Download orders as CSV
  → Query params: period, status, customer_id
  → Auth: admin+
```

### Inventory Endpoints

```
GET /inventory/items
  → List all inventory items with categories
  → Response: Array of item objects with SKU
  → Auth: any logged-in user

GET /inventory/categories
  → List unique categories
  → Response: Array of category strings
  → Auth: any logged-in user

GET /inventory/items/by-sku/<sku>
  → Lookup item by SKU (for barcode scanner)
  → Response: Single item object
  → 404 if not found
  → Auth: any logged-in user

POST /inventory/items
  → Create new inventory item
  → Body: { name, category, price, stock, description?, icon?, sku? }
  → Creates StockMovement for initial_stock
  → Auth: admin+

PUT /inventory/items/<id>
  → Update item (name, price, category, description, SKU, icon)
  → Body: { name, category, price, description?, sku?, icon? }
  → Stock NOT editable here
  → Auth: admin+

DELETE /inventory/items/<id>
  → Delete item
  → Auth: superuser only

POST /inventory/items/<id>/restock
  → Add stock to item
  → Body: { quantity, note? }
  → Creates StockMovement with reason "restock"
  → Auth: admin+

GET /inventory/low_stock
  → List items below threshold
  → Query params: threshold=5
  → Response: { threshold, count, items: [...] }
  → Auth: admin+

GET /inventory/movements
  → Stock audit log
  → Query params: item_id?, limit=50
  → Response: Array of StockMovement objects
  → Auth: admin+

POST /inventory/place_order
  → Create order from cart (main order flow)
  → Body: { items: [{id, quantity}, ...], customer_id? }
  → Creates Order, OrderItems, decrements stock, logs StockMovements, sends SMS
  → Auth: any logged-in user

GET /inventory/export
  → Download inventory as CSV
  → Query params: category?
  → Auth: admin+
```

### Customer Endpoints

```
POST /customers/register
  → Create new customer
  → Body: { name, phone_number, code, email? }
  → Auth: admin+

GET /customers/view_customers
  → List all customers (paginated)
  → Query params: page=1, per_page=50
  → Response: { customers: [...], pagination: {...} }
  → Auth: admin+

GET /customers/view_customers/<id>
  → Get customer detail with order history and stats
  → Response: { customer: {...}, orders: [...], stats: {...} }
  → Auth: admin+

PUT /customers/update_customers/<id>
  → Update customer details
  → Body: { name?, email?, phone_number?, code? }
  → Auth: superuser only

DELETE /customers/delete_customers/<id>
  → Delete customer (cascades orders)
  → Auth: superuser only
```

### User Endpoints

```
GET /users/view_users
  → List all users with filter by status/role
  → Query params: status?, role?
  → Response: Array of user objects
  → Auth: admin+

POST /users/approve/<user_id>
  → Approve user registration
  → Body: (empty)
  → Auth: admin+

POST /users/reject/<user_id>
  → Reject user registration
  → Body: (empty)
  → Auth: admin+

PUT /users/<user_id>/role
  → Change user role
  → Body: { role: "admin" or "user" }
  → Auth: superuser only
  → Cannot promote to superuser

DELETE /users/<user_id>
  → Delete user
  → Auth: superuser only
  → Cannot delete primary superuser

GET /users/pending_count
  → Get count of pending approvals (for badge)
  → Response: { count: N }
  → Auth: admin+
```

### Reports Endpoints

```
GET /reports/summary
  → Revenue KPIs for period
  → Query params: period=today|week|month|all
  → Response: { total_revenue, order_count, avg_order_value }
  → Auth: admin+

GET /reports/by_day
  → Daily revenue with gap-filling
  → Query params: days=30
  → Response: Array of { date, revenue, count }
  → Auth: admin+

GET /reports/top_items
  → Top selling items
  → Query params: limit=10
  → Response: Array of { name, qty, revenue }
  → Auth: admin+

GET /reports/status_breakdown
  → Order count by status
  → Response: { pending: N, confirmed: N, ... }
  → Auth: admin+

GET /reports/payment_breakdown
  → Revenue by payment method
  → Response: Array of { method, count, revenue }
  → Auth: admin+

GET /reports/export
  → Download revenue summary as CSV
  → Query params: days=30
  → Auth: admin+
```

### Frontend Routes (HTML)

```
GET /
  → Landing page with login button
  → Public (no auth required)

GET /dashboard
  → User dashboard (role-specific layout)
  → Auth: any logged-in user

GET /customers-ui
  → Customer management page
  → Auth: admin+

GET /orders-ui
  → Order management page
  → Auth: admin+

GET /inventory-ui
  → Inventory shop + management
  → Auth: any logged-in user

GET /users-ui
  → User approval & management
  → Auth: admin+

GET /reports-ui
  → Sales analytics page
  → Auth: admin+

GET /customers/<id>
  → Customer detail with order history
  → Auth: admin+

GET /customers/<id>/edit
  → Customer edit form
  → Auth: admin+

GET /orders/<id>/edit
  → Order edit form (item, amount, payment)
  → Auth: admin+

GET /orders/<id>/receipt
  → Printable order receipt
  → Auth: any (own orders only for users, all for admin)
```

---

## Database Schema

### Users Table
```
id              INTEGER PRIMARY KEY
email           VARCHAR(255) UNIQUE NOT NULL
name            VARCHAR(255)
phone_number    VARCHAR(20)
role            VARCHAR(20) DEFAULT 'user'  — superuser|admin|user
status          VARCHAR(20) DEFAULT 'pending'  — pending|approved|rejected
password_hash   VARCHAR(255)  — NULL for OAuth-only
created_at      DATETIME DEFAULT NOW()
```

### Customers Table
```
id              INTEGER PRIMARY KEY
name            VARCHAR(255) NOT NULL
email           VARCHAR(255)
phone_number    VARCHAR(15) UNIQUE NOT NULL
code            VARCHAR(50) UNIQUE NOT NULL
```

### InventoryItems Table
```
id              INTEGER PRIMARY KEY
name            VARCHAR(255) NOT NULL
description     VARCHAR(500)
price           DECIMAL(10,2) NOT NULL
category        VARCHAR(100) NOT NULL
stock           INTEGER DEFAULT 0
icon            VARCHAR(80)  — FontAwesome class
sku             VARCHAR(50) UNIQUE  — Barcode/SKU
```

### Orders Table
```
id                  INTEGER PRIMARY KEY
customer_id         INTEGER FOREIGN KEY → Customers.id
item                VARCHAR(255) NOT NULL  — legacy: item summary string
amount              DECIMAL(10,2) NOT NULL
status              VARCHAR(20) DEFAULT 'pending'  — pending|confirmed|completed|cancelled|refunded
payment_method      VARCHAR(20)  — cash|mpesa|card
payment_reference   VARCHAR(100)  — transaction code, etc.
time                DATETIME DEFAULT NOW()
```

### OrderItems Table
```
id                  INTEGER PRIMARY KEY
order_id            INTEGER FOREIGN KEY → Orders.id (CASCADE DELETE)
inventory_item_id   INTEGER FOREIGN KEY → InventoryItems.id (SET NULL)
item_name           VARCHAR(255) NOT NULL  — snapshot at order time
quantity            INTEGER DEFAULT 1
unit_price          DECIMAL(10,2) NOT NULL
subtotal            DECIMAL(10,2) NOT NULL
```

### StockMovements Table (Audit Log)
```
id                  INTEGER PRIMARY KEY
item_id             INTEGER FOREIGN KEY → InventoryItems.id (CASCADE DELETE)
item_name           VARCHAR(255) NOT NULL  — snapshot at movement time
delta               INTEGER NOT NULL  — +N for in, -N for out
reason              VARCHAR(50) NOT NULL  — sale|restock|cancel|refund|initial_stock|manual
user_id             INTEGER FOREIGN KEY → Users.id (SET NULL)
note                VARCHAR(255)  — optional detail
created_at          DATETIME DEFAULT NOW()
```

---

## Integrations

### Google OAuth 2.0

**Configuration:**
- Client ID & Secret in `.env` file
- Redirect URI: `{base_url}/auth/google-callback`
- Scopes: email, profile

**User Profile Data Captured:**
- Email (unique identifier)
- Name
- Picture URL

**Session Flow:**
1. User clicks "Sign in with Google"
2. Redirects to Google consent screen
3. User authorizes
4. Google redirects back to `/auth/google-callback`
5. System creates/updates user record
6. Session set with profile data
7. User redirected to dashboard

### Africa's Talking SMS

**Configuration:**
- API Key in `.env` file
- Shortcode (sender ID) configured in settings

**Order Confirmation SMS:**
- Triggered after order placement
- Recipient: customer phone number
- Message: "Order [ID] confirmed. Items: [items list]. Total: [amount]. Thank you!"
- Sent asynchronously after order commit

**Integration Points:**
- `/inventory/place_order` — sends SMS on success
- `/orders/place_order` (legacy) — sends SMS on success
- Failure logged but doesn't block order creation

**Error Handling:**
- SMS failure doesn't rollback order
- Warning logged to aid debugging
- Toast notification informs user if SMS failed

---

## Configuration

### Environment Variables (.env)

```
# Google OAuth
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_client_secret

# App Secret
APP_SECRET_KEY=your_random_secret_key_here

# Database (local development)
# SQLite by default - database.db auto-created
# For production, use MySQL/PostgreSQL with DATABASE_URL

# Africa's Talking
AFRICAS_TALKING_API_KEY=your_api_key
AFRICAS_TALKING_SHORTCODE=your_shortcode
AFRICAS_TALKING_USERNAME=your_username

# Business Info
BUSINESS_NAME=Make-An-Order Demo

# Admin Email (superuser)
SUPERUSER_EMAIL=kimothomark93@gmail.com

# Feature Flags
DEBUG=False  — Set to True for development
PORT=5001
```

### Constants in Code

- **LOW_STOCK_THRESHOLD** = 5 units (in `api/inventory.py`)
- **ITEMS_PER_PAGE** = 50 (default pagination)
- **SESSION_LIFETIME** = 24 hours
- **SUPERUSER_EMAIL** = 'kimothomark93@gmail.com' (protected)

---

## Deployment Notes

### Local Development Setup

1. **Clone repository**
   ```bash
   git clone <repo_url>
   cd make-an-order
   ```

2. **Create virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # or `venv\Scripts\activate` on Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your Google OAuth and Africa's Talking credentials
   ```

5. **Run application**
   ```bash
   python app.py  # or use run.py
   # Server starts on http://localhost:5001
   ```

6. **Database migration**
   - SQLite database auto-created on first run
   - Schema migrations run automatically
   - Seed data (sample inventory, customers) inserted on startup

### Production Deployment

**Key Considerations:**

1. **Database:** Switch to PostgreSQL or MySQL
   - Update `DATABASE_URL` in environment
   - Test migrations on staging first

2. **Secret Key:** Use strong, random `APP_SECRET_KEY`
   - Never hardcode or commit to version control

3. **HTTPS/SSL:** Use reverse proxy (nginx/Apache)
   - Redirect HTTP → HTTPS
   - Configure secure cookies

4. **Session Storage:** For multi-instance deployment
   - Use Redis or database-backed sessions
   - Ensure session persistence across instances

5. **SMS Integration:** Test error handling
   - Set `AFRICAS_TALKING_API_KEY` correctly
   - Monitor SMS delivery logs

6. **Google OAuth:** Register production domain
   - Add redirect URI to Google Console
   - Update `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`

7. **Logging:**
   - Enable rotating file handler (enabled in non-debug mode)
   - Monitor `logs/make-an-order.log`

8. **Performance:**
   - Add database indexes on frequently queried columns (customer_id, status, sku)
   - Consider caching inventory/category data (Redis)
   - Use connection pooling for database

### Docker Deployment (Optional)

Create `Dockerfile`:
```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FLASK_APP=app.py
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "app:app"]
```

Build & run:
```bash
docker build -t make-an-order .
docker run -p 5001:5001 -e APP_SECRET_KEY="..." -e GOOGLE_CLIENT_ID="..." ... make-an-order
```

---

## Feature Checklist

### Core POS Features
- [x] Cart-based order placement
- [x] Inventory stock management
- [x] Real-time stock decrements on sale
- [x] Stock restoration on cancel/refund
- [x] Order status lifecycle (pending → completed/cancelled/refunded)
- [x] Payment method tracking (cash, M-Pesa, card)
- [x] Customer linking to orders

### User Management
- [x] Google OAuth 2.0 integration
- [x] Multi-role RBAC (superuser, admin, user)
- [x] User registration workflow
- [x] Admin approval of new users
- [x] User role management (promote/demote)

### Inventory Management
- [x] Full CRUD for inventory items
- [x] Category organization
- [x] Dynamic pricing
- [x] Stock audit trail (StockMovement log)
- [x] Low-stock alerts
- [x] Barcode/SKU scanning
- [x] Text-based search

### Reporting & Analytics
- [x] Revenue KPIs (total revenue, order count, avg order value)
- [x] Revenue trends (daily breakdown)
- [x] Top-selling items
- [x] Order status distribution
- [x] Payment method analysis
- [x] Time-period filtering (today, 7 days, 30 days, all time)
- [x] CSV export for all reports

### Receipts & Exports
- [x] Printable order receipts
- [x] CSV export: orders
- [x] CSV export: inventory
- [x] CSV export: reports
- [x] Print-optimized styling

### Integrations
- [x] Google SSO
- [x] Africa's Talking SMS confirmations
- [x] SMS on order placement

### UI/UX
- [x] Responsive Bootstrap 5 design
- [x] Toast notifications
- [x] Modal forms
- [x] Bootstrap tabs
- [x] Dynamic tables with pagination
- [x] Chart.js integration (line, doughnut, progress bars)
- [x] Search & filter
- [x] Badge indicators (stock, status, roles)

---

## Known Limitations & Future Enhancements

### Current Limitations

1. **Session Storage:** In-memory Flask sessions (not distributed)
2. **Rate Limiting:** None implemented yet
3. **Inventory Sync:** No multi-location support
4. **Customer Search:** Stub only (filter on client-side)
5. **Refund Processing:** Manual (no auto-refund integration)

### Planned Enhancements

1. **Barcode Printing:** Generate & print item barcodes
2. **Advanced Analytics:** Predictive stock levels, sales forecasting
3. **Customer Segments:** Loyalty program, customer tiers
4. **Multi-Location:** Support multiple warehouses/branches
5. **Mobile App:** Native app for point-of-sale terminals
6. **API Rate Limiting:** Prevent abuse
7. **Webhooks:** Real-time event notifications
8. **Bulk Operations:** Import/export via Excel
9. **Custom Reports:** User-defined report templates
10. **Discount/Promotion System:** Apply discounts and promos at checkout

---

## Support & Maintenance

### Troubleshooting

**Issue: "User not found" on login**
- Check user email matches registered Google account
- Verify SUPERUSER_EMAIL constant if first-time setup

**Issue: Orders not decrementing stock**
- Verify order created via `/inventory/place_order` (not legacy endpoint)
- Check StockMovement records in audit log
- Confirm InventoryItem linked to OrderItem

**Issue: SMS not sending**
- Verify Africa's Talking API key in `.env`
- Check customer phone number format (include country code)
- Review application logs for SMS send errors
- Test with hardcoded recipient in staging

**Issue: Pagination showing empty results**
- Check page number is <= total pages
- Verify per_page parameter is valid (1-200)
- Ensure records exist in time range being queried

### Performance Tips

1. Add indexes on frequently queried columns:
   ```sql
   CREATE INDEX idx_order_customer ON orders(customer_id);
   CREATE INDEX idx_order_status ON orders(status);
   CREATE INDEX idx_order_time ON orders(time);
   CREATE INDEX idx_inventory_sku ON inventory_items(sku);
   CREATE INDEX idx_sku_movement_item ON stock_movements(item_id);
   ```

2. Archive old orders to separate table after 1 year
3. Cache inventory/categories in Redis (5-min TTL)
4. Use database connection pooling
5. Optimize images/icons for fast loading

---

## License & Attribution

- Built with Flask 3.0, SQLAlchemy 2.0, Bootstrap 5, Chart.js 4.4.3
- Google OAuth via Authlib
- SMS via Africa's Talking API
- Hosting-ready for Render, Heroku, AWS, or self-hosted

**Last Updated:** 2026-03-30
**Version:** 1.0.0
