---
layout: post
title: "Building a POS System: What I Did Wrong and How I Fixed It"
date: 2026-03-30 10:00:00
description: Mistakes made while building a real ordering system and what I learned from having to rewrite stuff
tags: projects flask backend retail python
categories: projects
giscus_comments: true
related_posts: true
---

I was building a POS system for someone who actually needed to use it. Not a tutorial. Not a demo. A real system where someone takes orders and tracks inventory.

I made mistakes. Some of them cost me hours of work to fix. But that's the best way to learn because you're fixing real problems, not theoretical ones.

## The First Problem: Auto-Provisioning Customers

I thought: when a staff member places an order, they become a customer automatically. I'll just create a customer record linked to their user account. Problem solved.

So I wrote this:

```python
def place_order(user_id, items):
    user = User.query.get(user_id)

    # Auto-create customer if they don't have one
    if not user.customer_code:
        user.customer_code = f"USR-{user_id}"

    order = Order(user_id=user_id, items=items)
    db.session.add(order)
    db.session.commit()
```

This worked for about five minutes. Then I showed it to someone and said: "Here's the report showing your top customers."

At the top of the list:
- USR-1 (staff testing the system)
- USR-2 (another staff member)
- USR-3 (me testing)

The actual customers who paid money? Buried below all the staff test data.

Worse, the superuser had placed 16 orders just testing things. So USR-1 looked like the biggest revenue source. Wrong. It was me testing.

The "customers" report was useless because staff was polluting it.

## The Real Problem: Two Different Things

I was trying to squeeze two different concepts into one model:

**A User:** Someone who logs in to manage the system. Email. Google OAuth. Role-based permissions (admin, operator, etc).

**A Customer:** Someone who buys things. Phone number. Gets SMS notifications. No email login. No role. Just buys stuff.

These are different. Not just slightly different. Fundamentally different purposes. Different data. Different workflows.

But I had one User table handling both. That's when things got messy.

## How This Actually Broke Things

**Reports Broken**
```python
# Show top customers by revenue
SELECT customer_code, SUM(amount)
FROM orders
GROUP BY customer_code
ORDER BY revenue DESC
```

At the top: USR codes (staff accounts). Real customers buried below. Report useless.

**Analytics Wrong**
Any metric about "customers" included staff test accounts. Revenue calculations wrong. Repeat purchase rates wrong. Everything was noise.

**UI Confusion**
The staff would see themselves in the customer list. "Why am I listed as a customer?"

**Ordering Broken**
When an admin wanted to place an order for an actual customer, the system was confused. The order got linked to the admin's auto-customer account instead of the real customer.

## What I Did to Fix It

Separated them completely.

```python
class User(db.Model):
    """Staff member or system operator"""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    name = db.Column(db.String(120))
    role = db.Column(db.String(20))  # admin, user, superuser
    created_at = db.Column(db.DateTime)

class Customer(db.Model):
    """Buyer. Completely different from User"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)  # CUST-001
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))  # For SMS
    created_at = db.Column(db.DateTime)

class Order(db.Model):
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))  # WHO bought
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))  # WHO recorded it
```

Now:
- User is for staff
- Customer is for buyers
- Order knows both: which customer bought it, which staff person processed it

No auto-provisioning. No confusion. No USR codes polluting the data.

## What Changed in the UI

With separate tables, the login flow had to change.

Before: everyone logs in one place.

After:
```
/role-select page
  ↓
Choose "Staff Member" or "Customer"
  ↓
Different flows for each
```

Staff member goes to Google OAuth. Customer enters their code or email. Guests can just browse.

The entire checkout flow changed:

Before:
```javascript
// Implicitly link to current user
order = place_order({ items: [...] })
// Assumes current_user is the customer
```

After:
```javascript
// Admin explicitly picks which customer
const customerId = document.getElementById('customer-selector').value;
order = place_order({ customer_id: customerId, items: [...] });
// No assumptions
```

For an operator (non-admin):
```javascript
// Their orders still work
order = place_order({ items: [...] });
// Backend knows it's their own customer record
```

## What Broke During the Change

I had to migrate data.

All those SQLite USR-1, USR-2 records? I converted them to proper Customer records:

```python
for user in User.query.all():
    if user.role not in ['admin', 'superuser']:
        customer = Customer(
            code=f"USR-{user.id}",
            name=user.name,
            phone=user.phone
        )
        db.session.add(customer)
```

Then reassigned all orders:

```python
for order in Order.query.all():
    customer = Customer.query.filter_by(code=f"USR-{order.user_id}").first()
    if customer:
        order.customer_id = customer.id
```

Took an hour. Tested it thoroughly. Nothing broke after migration.

## Backend Had to Validate Everything

The order endpoint changed from trusting context to requiring explicit data:

```python
@admin_api_required
def place_order():
    customer_id = request.json['customer_id']  # Required, explicit
    customer = db.session.get(Customer, customer_id)

    if not customer:
        return {'error': 'Invalid customer'}, 400

    order = Order(
        customer_id=customer_id,
        created_by=current_user.id,
        items=request.json['items']
    )
```

No more implicit assumptions. Everything is explicit. That makes bugs obvious.

## Why This Matters

Before the change: report shows staff, code has special cases for USR handling, login is ambiguous.

After: reports show real customers, code is straightforward, login is explicit.

The separation also made permissions clearer. A superuser manages Users. An admin manages Customers. These are separate concerns in separate parts of the code.

## What I Learned

Two things that look similar often need to be completely separate. Just because they share some fields (name, created_at) does not mean they should be in the same table.

When you conflate them, you end up dealing with:
- Confusing code (is this field about staff or about buyers?)
- Special cases (this endpoint behaves differently if role is X)
- Wrong reports (data you should not be seeing shows up)
- Migrations (when you finally separate them, you have to rewrite everything)

The cost of getting it wrong is higher than getting it right from the start.

If you're building something similar, think clearly about what your entities are. A staff member is not a customer. A user is not a buyer. Keep them separate. From the beginning.

It takes maybe five minutes longer to set up separate tables. And it saves you hours later when you're not debugging weird edge cases.

---

The code is on [GitHub](https://github.com/Markkimotho/make-an-order) if you want to see how it's structured now.
