---
layout: post
title: "The Architecture Mistake That Took Three Rewrites to Fix: Users vs Customers"
date: 2026-03-31 10:00:00
description: Why collapsing staff accounts and customer accounts into one table looked elegant but broke everything, and how I finally got the separation right.
tags: architecture database design oop refactoring lessons python sqlalchemy
categories: projects
giscus_comments: true
related_posts: true
---

There is an architecture mistake so common it has its own pattern: the false normalization. You look at your data model, see overlap, and think: "I can make this simpler."

Then you ship it. And it breaks in ways you did not anticipate. Then you have to rewrite it while the system is live.

This is the story of that mistake.

## The Elegant Wrong Design

I started with one `User` table. Everyone was a user. Staff who logged in. Customers who placed orders. The table had:

```python
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    role = db.Column(db.String(20), default='customer')  # customer, admin, superuser
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

A staff member is a user with `role='admin'`. A customer is a user with `role='customer'`. Simple. One table. One query. Database normalization achieved.

Except this was wrong.

## Where It Started Breaking

The first crack appeared when an order endpoint needed to reference who placed it. I added:

```python
order = Order(
    user_id=current_user.id,
    items=items,
    total=total
)
```

Now every order knows which "user" placed it. But what does that mean?

- If a staff member placed it, they are not the customer. The actual customer should be someone else.
- If a customer placed it, they are the user.
- If a superuser placed it to test the system? That should not be an order at all.

The system was collapsing two completely different use cases into one model.

## The Auto-Provisioning Disaster

To paper over the cracks, I added auto-provisioning. When a non-admin user placed an order:

```python
def place_order(user_id, items):
    user = User.query.get(user_id)

    # Auto-create a customer record if they do not have one
    if not user.customer_code:
        user.customer_code = f"USR-{user_id}"
        db.session.add(user)

    order = Order(
        user_id=user_id,
        customer_code=user.customer_code,
        items=items
    )
    db.session.add(order)
    db.session.commit()
```

Now every user who places an order gets a customer code. Clever. Solves the problem. For about five minutes.

Then I needed to build an analytics dashboard. "Show me top customers by spending." I query:

```sql
SELECT customer_code, SUM(total) as revenue
FROM orders
GROUP BY customer_code
ORDER BY revenue DESC
```

At the top: `USR-1`, `USR-2`, `USR-3`. Who are these? Oh. Staff accounts. The superuser was placed sixteen orders while testing. The admin created five orders to verify the system. Now the "top customers" are actually staff members.

The report is useless.

## The Moment I Realized

I was showing the system to someone who wanted to use it. They looked at the orders list and saw:

- Order #124 from "john@company.com" ($500)
- Order #125 from "jane@company.com" ($300)

"Wait, why are our staff placing orders?"

"They are not. These are actual customers."

"But I recognize these emails. These are my managers. They should not be in the customer list."

He was right. My auto-provisioning had turned internal staff accounts into phantom customers. His actual customers — the people paying money — were hidden in a list polluted with test accounts.

I had confused the system into uselessness.

## The Separation

I stopped trying to be clever and just separated them:

```python
class User(db.Model):
    """A staff member or platform operator."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True)
    name = db.Column(db.String(120))
    role = db.Column(db.String(20))  # admin, user, superuser
    created_at = db.Column(db.DateTime)

class Customer(db.Model):
    """A buyer or client."""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True)  # CUST-001, etc
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))  # For SMS
    email = db.Column(db.String(120))
    created_at = db.Column(db.DateTime)

class Order(db.Model):
    """An order placed by a customer, recorded by a user."""
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))  # WHO bought
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))  # WHO recorded it
    items = db.relationship('OrderItem')
    total = db.Column(db.Float)
    created_at = db.Column(db.DateTime)
```

Now the model is explicit:
- **User** is a staff account. Only staff have these.
- **Customer** is a buyer. Only buyers have these.
- **Order** knows both: which customer bought, which staff member processed the order.

An unpaid invoice is on a customer. A user never has unpaid invoices attached to them. A user never shows up in "top customers." An admin placing a test order can link it to a dummy "TEST" customer instead of polluting real customer data.

## The Cascading Fixes

But separating the tables was the trivial part. Once you separate them, you have to rethink everything built on top.

### Login Flow

Before: "Anyone who logs in is a user."

After: Different login flows for different entity types.

```
/login?role=staff
  ↓
Google OAuth
  ↓
Create/update User record
  ↓
Dashboard for staff

/login?role=customer
  ↓
Code/email lookup
  ↓
Load Customer record (already exists, created by admin)
  ↓
Shop for customer
```

No more ambiguity. A login creates a User or loads a Customer. Clear semantics.

### Order Placement

Before:
```python
order = Order(user_id=current_user.id, ...)  # User IS the customer
```

After:
```python
@admin_api_required  # Admins can place on behalf of anyone
def place_order():
    customer_id = request.json['customer_id']  # Explicit
    order = Order(customer_id=customer_id, created_by=current_user.id)

    # Audit trail: who did this action?
```

Admin can place an order for any customer. The order is linked to the customer, not the admin. The audit trail remembers the admin did it.

### Analytics

Before: Top customers included staff. Useless.

After:
```sql
SELECT c.name, SUM(o.total) as revenue
FROM customers c
JOIN orders o ON c.id = o.customer_id
WHERE o.created_at > DATE_SUB(NOW(), INTERVAL 30 DAY)
GROUP BY c.id
ORDER BY revenue DESC
```

Now it is actually customers. Clean. Accurate.

## The Template Logic

But database schema is infrastructure. The user sees the UI. Once users and customers are separate, the UI logic cascades:

```html
{% if user_type == 'staff' %}
  <!-- Inventory page shows customer selector -->
  <select id="customer-selector">
    {% for customer in all_customers %}
      <option value="{{ customer.id }}">{{ customer.name }}</option>
    {% endfor %}
  </select>
{% elif user_type == 'customer' %}
  <!-- Customer sees only their own items -->
  <button>Add to cart</button>
{% else %}
  <!-- Guest sees nothing clickable -->
  <p>Sign up to order</p>
{% endif %}
```

Before, the logic had to check `user.role == 'customer'` and do weird things. Now it is: "Is this a customer or staff?" One check. Clear branching.

## The API Permissions Layer

Before:
```python
@login_required
def place_order():
    # Check role: admin gets special powers
    if current_user.role == 'admin':
        customer_id = request.json.get('customer_id')
    else:
        customer_id = current_user.id  # Assume user is customer

    # This is fragile. What if current_user.id does not correspond to a customer?
```

After:
```python
@admin_api_required  # Only staff with admin role
def place_order():
    customer_id = request.json['customer_id']  # Required
    customer = db.session.get(Customer, customer_id)
    assert customer, "Invalid customer"

    order = Order(customer_id=customer_id, created_by=current_user.id)

    # Every field is explicit. Types match reality.
```

No ambiguity. No checking some object to see if it might be a customer. A customer is a Customer. A user is a User.

## The Pain of Migration

Once I realized the old design was broken, I had to migrate live data.

All those `USR-{id}` records? I converted them to proper Customer records:

```python
for user in User.query.all():
    if user.role != 'admin' and user.role != 'superuser':
        # This is a customer-like user. Create a proper customer
        customer = Customer(
            code=f"USR-{user.id}",
            name=user.name,
            phone=user.phone,
            email=user.email
        )
        db.session.add(customer)

db.session.commit()
```

Then I reassigned all orders:

```python
for order in Order.query.all():
    # Find the customer for this order
    customer = Customer.query.filter_by(code=f"USR-{order.user_id}").first()
    if customer:
        order.customer_id = customer.id
    # else: we lost the link

db.session.commit()
```

Nothing broke. Except for an hour of careful work and testing.

## What This Taught Me

**Conflating two entity types wastes time later.** I spent weeks working around the bad design before I finally fixed it. I could have gotten it right in two hours if I had thought it through.

**Just because it is possible does not mean it is a good idea.** I could have one table for users and customers. But should I? No. One table created a thousand edge cases.

**Clear domain models prevent bugs.** Once User and Customer were separate, the code was simpler. The logic was obvious. The bugs went away.

**Schema design is not just about storage — it is about semantics.** A schema is how you tell the code what is true about your world. "Staff and customers are different" is an important truth. The schema should reflect it.

## The Messy Reality

I still have one place where they touch:

```python
# A user can be linked to a customer account
class User(db.Model):
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
```

This lets a staff member also have a customer account and place personal orders. It is optional. Clean.

But it is the exception. The rule is: Users are staff. Customers are buyers. They are different. The code and schema say so.

## In Retrospect

I should have asked: "What is the difference between these two types of people?" from the start. A user is someone who operates the system. A customer is someone who uses the system to buy things. Different motivations. Different data. Different workflows.

But this is obvious only after you have paid the price of conflating them.

If you are designing a system and you see two different actors with overlapping data, separate them. Name them differently. Put them in different tables. Let the schema guard against accidental mixing.

It will feel like extra work at first. It will save ten times that effort when you do not have to special-case your logic to work around a broken design.

The code is on [GitHub](https://github.com/Markkimotho/make-an-order). Look at how orders, users, and customers relate. See how clean the separation makes everything.

Then go look at a codebase where someone conflated two entity types. You will see the difference immediately. The compromises. The edge cases. The checks that should not need to exist.

Do not be that codebase. Separate your concerns. From the start. Your future self will be grateful.
