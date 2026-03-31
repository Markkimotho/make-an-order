---
layout: post
title: "Data Model Mistakes Cost Hours. Getting It Right From the Start Takes 20 Minutes."
date: 2026-03-31 10:00:00
description: Why I had to rewrite my user and customer system, step by step, and what that taught me about schema design
tags: architecture database design sqlalchemy python
categories: projects
giscus_comments: true
related_posts: true
---

I made a database design mistake. Not a catastrophic one. But the kind that costs you time later and forces a rewrite while the system is live.

The mistake: trying to use one table for two completely different types of things.

Let me walk through exactly what happened and what I learned.

## The Initial Design

I thought about it like this: there are entities in the system. They all have common properties (id, name, created_at). So I'll make one User table and use a role field to distinguish between them.

```python
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120))
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    role = db.Column(db.String(20), default='customer')  # customer, admin, superuser
    created_at = db.Column(db.DateTime)
```

A staff member? `role='admin'`. A customer? `role='customer'`. One table. Clean.

Except it was not clean. It was hiding problems.

## Where the Problems Started

**Problem 1: What does phone mean?**

If it's a staff member, phone might be their work phone. If it's a customer, phone is for SMS notifications. Same field. Different meanings.

**Problem 2: What does email mean?**

Staff members use email to log in. Customers don't have email logins in this system. They log in with a code. So why is email a required field?

**Problem 3: What's a customer code?**

I added `customer_code` field to handle auto-provisioning. When staff placed orders, they got `USR-{id}`. But staff also have emails. Customers have codes but not emails. The table was holding data that only applies to certain rows.

**Problem 4: Orders got confusing**

When I created the Order table, I linked it to User:

```python
class Order(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
```

Now what does this mean? Did a staff member place this order? Did a customer? If an admin placed an order for someone else, which user_id gets stored? The admin who processed it or the "customer" user who has the order?

I had to guess at semantics. The table was unclear.

## The Cascade of Workarounds

To deal with the confusion, I added code to handle special cases:

```python
def get_customer_for_user(user):
    """Get the customer record for this user, or create one if needed"""
    if user.role == 'admin' or user.role == 'superuser':
        return None  # These are not customers

    if not user.customer_code:
        user.customer_code = f"USR-{user.id}"
        db.session.add(user)

    # Now find/create a customer record somehow
    customer = Customer.query.filter_by(code=user.customer_code).first()
    if not customer:
        customer = Customer(code=user.customer_code, name=user.name)
        db.session.add(customer)

    return customer
```

This function checks role, handles special cases, creates phantom records. Every place that needed to know "who is the customer" had to call this and deal with None returns.

The codebase was filling up with patches on top of a bad foundation.

## Analytics Stopped Working

The big breaking point was reporting.

I built a "top customers" report:

```python
def top_customers():
    return db.session.query(
        User.customer_code,
        func.count(Order.id).label('order_count'),
        func.sum(Order.total).label('revenue')
    ).join(Order).filter(
        User.role == 'customer'
    ).group_by(
        User.customer_code
    ).order_by(
        func.sum(Order.total).desc()
    ).limit(10)
```

The results:
1. USR-1 - $2000 revenue (me testing)
2. USR-2 - $1500 revenue (another staff member testing)
3. USR-3 - $1200 revenue (superuser placing test orders)
4. ... (actual customers way down the list)

The system was reporting phantom staff accounts as top customers. The analytics were garbage because the data model was unclear about what a customer actually was.

## Migration Blew It Up

At some point, I realized I had to clean the data. I could not keep operating with US auto-provisioned accounts polluting everything.

I had two options:

1. Keep patching the code and hoping it works
2. Separate User and Customer into different tables

Option 1 meant indefinite complexity. Option 2 meant a migration.

I did the migration.

## Step 1: Create the Separate Tables

```python
class User(db.Model):
    """A staff member"""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(120))
    role = db.Column(db.String(20))  # admin, user, superuser
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Customer(db.Model):
    """A buyer"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))  # For SMS
    email = db.Column(db.String(120))  # Optional contact
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Order(db.Model):
    """Links a customer (who bought) and a user (who processed)"""
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))  # Staff who put it in
    total = db.Column(db.Float)
    created_at = db.Column(db.DateTime)
```

Now the tables are explicit. User holds staff. Customer holds buyers. Order has two relationships: who bought it (customer) and who processed it (user).

## Step 2: Migrate Existing Data

I had old User records where role was 'customer'. These needed to become Customer records.

```python
from datetime import datetime

users_to_migrate = User.query.filter(User.role == 'customer').all()

for user in users_to_migrate:
    # Create a customer record from this user
    customer = Customer(
        code=user.customer_code or f"USR-{user.id}",
        name=user.name,
        phone=user.phone,
        email=user.email,
        created_at=user.created_at
    )
    db.session.add(customer)

db.session.commit()

# Store the mapping so we can update orders
user_to_customer = {}
for user in users_to_migrate:
    customer = Customer.query.filter_by(
        code=user.customer_code or f"USR-{user.id}"
    ).first()
    if customer:
        user_to_customer[user.id] = customer.id
```

## Step 3: Update Orders Table

Orders had `user_id`. Now they need `customer_id` and `created_by`.

```python
for order in Order.query.all():
    if order.user_id in user_to_customer:
        order.customer_id = user_to_customer[order.user_id]
        order.created_by = order.user_id  # The "user" became the processor
    db.session.add(order)

db.session.commit()
```

## Step 4: Delete Old User Records

Once everything migrated, delete the customer-role users.

```python
User.query.filter(User.role == 'customer').delete()
db.session.commit()
```

Took about an hour total. Tested everything. No data loss.

## Step 5: Update the Code

Now the code became straightforward.

Before (messy):
```python
def place_order(user):
    customer = get_customer_for_user(user)  # Guess and special case
    if not customer:
        customer = auto_provision_customer(user)  # Create one somehow

    order = Order(user_id=user.id, ...)  # Ambiguous
```

After (clear):
```python
@admin_api_required
def place_order():
    customer_id = request.json['customer_id']  # Explicit
    customer = db.session.get(Customer, customer_id)  # Direct lookup

    order = Order(
        customer_id=customer_id,  # Clear: who is buying
        created_by=current_user.id,  # Clear: who is processing
        ...
    )
```

No special cases. No ambiguity. Just straightforward database operations.

## Step 6: Fix the Reports

Now the analytics worked:

```python
def top_customers():
    return db.session.query(
        Customer.name,
        func.count(Order.id).label('order_count'),
        func.sum(Order.total).label('revenue')
    ).join(Order).group_by(
        Customer.id
    ).order_by(
        func.sum(Order.total).desc()
    ).limit(10)
```

This only touches the Customer table. No phantom staff accounts. Just real customers who bought things.

## Step 7: Update the Frontend

The UI had to change because staff and customers now have separate login flows.

```html
<div class="role-select">
  <a href="/login?role=staff">Staff Sign In</a>
  <a href="/login?role=customer">Customer Login</a>
</div>
```

Staff goes through Google OAuth. Customer enters their code. Different flows. Clear intent.

The inventory page showed conditional fields:

```html
{% if user_type == 'staff' %}
  <select name="customer_id">
    {% for customer in all_customers %}
      <option value="{{ customer.id }}">{{ customer.name }}</option>
    {% endfor %}
  </select>
{% endif %}
```

Staff sees a dropdown (pick which customer). Customer sees nothing (it's them). Guest sees nothing.

## What This Fixed

**Reports** — Now actually show real customers. No staff test data pollution.

**Code clarity** — No more `get_customer_for_user()` function. No special cases. Just query the right table.

**Permissions** — A super clear separation. Users belong under user management. Customers belong under customer management. No overlap.

**API contracts** — Endpoints take `customer_id` explicitly. No guessing about who's the buyer.

**Data integrity** — Staff and customers can never accidentally mix because they're different tables.

## The Principle

Your database schema is not just storage. It's how you tell the code what is true about your system.

If you say "staff and customers are the same table," you're telling the code "these are interchangeable." But they're not. Staff manages. Customers buy. Different responsibilities. Different data.

Separating them into different tables is not just cleaner code. It's honest about what these entities actually are.

## The Cost

The rewrite took one hour of careful migration. But I had to stop shipping features while I did it. That was one hour I did not spend on new features.

If I had designed it correctly from the start (which would have taken five minutes extra), I would not have lost that hour.

And I would not have written all the special-case code and workarounds that I then had to rip out.

The investment in getting the schema right up front pays dividends immediately.

## For Your Next Project

When you're designing your schema, ask: "What are the different types of things in this system?"

For each type, ask: "What data do they have? What can they do? Are they the same type as another, or different?"

If they're different, separate them. Give them their own table. Give them their own code paths.

It feels like extra work. It's actually a time saver.

---

Code is on [GitHub](https://github.com/Markkimotho/make-an-order) if you want to see the current structure. Look at how orders, users, and customers relate. That's the clarity I'm talking about.
