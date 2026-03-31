---
layout: post
title: "Building a POS System with Flask: Order Management and Real-Time Inventory"
date: 2026-03-30 10:00:00
description: How I built an ordering system that tracks inventory, manages customers, and sends SMS notifications
tags: projects flask backend retail python inventory
categories: projects
giscus_comments: true
related_posts: true
---

I built a point of sale system for managing retail orders and inventory. It needed to handle orders placed by staff on behalf of customers, track stock in real-time, send SMS confirmations, and provide visibility into what's been sold.

Here's how it works.

## The Core Flow

When an admin opens the inventory page, they see a list of products. At the top is a dropdown to select which customer they're placing this order for. They click products to add them to a cart, adjust quantities, then click place order.

The order goes into the database with status `pending`. The customer's phone number gets pulled from the customer record. An SMS fires off: "Order placed. Total: $100. Reference: ORD-1234."

The order stays in pending until admin confirms it's ready (status: `confirmed`), then marks it complete. If something goes wrong, they can cancel it. When cancelled, the stock added back and status becomes `cancelled`.

## Managing Stock

Every product has a quantity field. When an order is placed, the quantity decreases immediately by the sum of items in that order. This keeps the inventory page showing accurate stock.

There's a separate restock endpoint for when new inventory arrives. Admin adds quantity and the stock count goes up.

If an order gets cancelled or refunded, the stock is restored. This happens through a stock movement record that logs the change.

## The Stock Audit Trail

Every time inventory changes—whether it's a sale, restock, cancellation, or refund—a record goes into `stock_movements`:

```python
StockMovement(
    inventory_id=product.id,
    movement_type='sale',
    quantity_changed=-5,
    reason='order #123',
    created_by=admin_user.id,
    created_at=datetime.utcnow()
)
```

This table is queryable. You can ask: "Show me all changes to Product X" or "Who restocked this item?"  If inventory doesn't match, you trace back through the audit trail and find where the discrepancy happened.

## The Order Lifecycle

Orders start as `pending`. From there:

- **To confirmed:** Admin reviews and confirms they can fulfill it
- **To completed:** Order was fulfilled and delivered
- **To cancelled:** Order aborted, stock restored
- **To refunded:** Order refunded to customer, stock restored

The status flows are simple but cover the main cases. An order completed or cancelled can't change status.  Once something is fulfilled or cancelled, it's done.

## Customer Management

Customers are separate from staff. Each has a unique code (CUST-001, etc). Admin creates customer records with name, phone number, and code.

When someone places an order, they specify which customer it's for. The order links to the customer record, not the staff member.

This keeps customers distinct from staff accounts. The admin using the system doesn't show up in customer analytics or the customer list.

## Guests and Operators

The system handles different user types:

**Admin** — Places orders, manages customers, sees all orders and reports

**Operator** — Can place orders but limited. The order automatically links to their own customer account (if they have one)

**Guest** — Can browse the inventory page without logging in but can't place orders

The inventory page is the same for all three. Guest sees products but the place order button is disabled. Operator sees it enabled. Admin sees a customer selector dropdown plus the button.

Validation happens at the API level too. An unauthenticated request to place_order returns 401. A guest or operator can't pass a different customer_id in the request.

## SMS Integration

When an order is placed, the system sends an SMS to the customer's phone number. This uses Africa's Talking API.

The SMS is wrapped in try/catch. If the API is down, the phone number is invalid, or the API key is wrong, the order still goes through. SMS is a nice-to-have notification, not a blocker for the sale.

```python
try:
    send_sms(customer.phone, f"Order {order.id} placed. Total: ${order.total}")
except Exception as e:
    logger.warning(f"SMS send failed: {e}")
```

This way the customer still has their order even if the notification doesn't go out.

## Reports

Three main reports are available: revenue over time, top products by quantity sold, and payment method breakdown.

Revenue uses date grouping:

```python
db.session.query(
    func.date(Order.created_at).label('date'),
    func.sum(Order.total_amount).label('revenue'),
    func.count(Order.id).label('order_count')
).group_by(
    func.date(Order.created_at)
).order_by('date')
```

This shows daily revenue and number of orders per day. You can filter by date range.

Top products groups by inventory item and sums quantity:

```python
db.session.query(
    Inventory.name,
    func.sum(OrderItem.quantity).label('qty_sold'),
    func.sum(OrderItem.subtotal).label('revenue')
).join(OrderItem).group_by(
    Inventory.id
).order_by(func.sum(OrderItem.quantity).desc())
```

Shows which items move the most and how much revenue they generated.

Payment method breakdown groups orders by their payment_method field (cash, card, mobile, etc):

```python
db.session.query(
    Order.payment_method,
    func.count(Order.id).label('count'),
    func.sum(Order.total_amount).label('total')
).group_by(Order.payment_method)
```

All three are rendered as charts on the reports dashboard.

## The Database

The Order table structure:

```python
class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    total_amount = db.Column(db.Float)
    status = db.Column(db.String(20))  # pending, confirmed, completed, etc
    payment_method = db.Column(db.String(50))
    payment_reference = db.Column(db.String(100))
    created_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime)
```

Each order has order items in a separate table:

```python
class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'))
    inventory_id = db.Column(db.Integer, db.ForeignKey('inventory.id'))
    quantity = db.Column(db.Integer)
    unit_price = db.Column(db.Float)
    subtotal = db.Column(db.Float)
```

This way an order can have multiple items and you can see what was sold, at what price, and in what quantity.

## Receipts

Once an order is placed, you can generate a receipt. The receipt includes:

- Order ID and date
- Customer name and phone
- Line items (product name, quantity, unit price, subtotal)
- Total
- Payment method and reference
- Status

It's a simple HTML template that can be printed or downloaded as PDF.

## Placing an Order Programmatically

The endpoint takes JSON:

```json
{
  "customer_id": 5,
  "items": [
    {"inventory_id": 12, "quantity": 2},
    {"inventory_id": 18, "quantity": 1}
  ],
  "payment_method": "card",
  "payment_reference": "TXN12345"
}
```

The endpoint:
1. Validates customer exists
2. Validates each item exists and has stock
3. Creates the order
4. Creates order items
5. Decreases inventory
6. Creates stock movement records
7. Sends SMS
8. Returns the order ID

If any step fails (invalid customer, out of stock, SMS error), the request returns an error. Stock is only decreased if the order is successfully created.

## Built to Actually Work

This isn't theoretical. Orders placed here go into a real database. Stock counts are accurate because they change in real-time. SMS notifications actually happen. Reports show what actually drove revenue.

It's a working POS system. Nothing fancy. Just built to do what it needs to do.

---

The code is on [GitHub](https://github.com/Markkimotho/make-an-order).
