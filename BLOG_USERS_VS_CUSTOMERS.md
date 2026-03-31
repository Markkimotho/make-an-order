---
layout: post
title: "Separating Users from Customers: Building a Multi-Role POS System"
date: 2026-03-31 10:00:00
description: How a point of sale system separates staff accounts from customer records to keep operations clean and let each role do what it needs
tags: architecture database design python sqlalchemy
categories: projects
giscus_comments: true
related_posts: true
---

Building a system where both staff and customers use it means deciding early: are they the same type of entity or different?

In a POS system, they're different. Here's why and how I built it that way.

## The Classification Problem

A **User** in this system is a staff member. They have an email, log in via Google OAuth, belong to a role (admin, operator, superuser). They manage the system.

A **Customer** is a buyer. They have a name, phone number, a unique code. They place orders. They get SMS notifications. They don't manage anything.

These are two different types of people with different needs and different data. Putting them in the same table creates problems.

## Why Not One Table

If you use one User table with a role field, you get confusion:

- What's the phone number for? Staff contact info or customer SMS? Both?
- Email is required for Google OAuth login. But customers don't log in with email. Should email still be required?
- When you make a report on "top customers," which records do you filter on? role='customer'? Then staff test orders pollute the results.
- When an admin places an order, should it link to their user record or to an actual customer record?

The table starts holding data that only applies to some rows. You add special case code everywhere. Logic gets tangled.

## The Separation

The system has two tables:

```python
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(120))
    role = db.Column(db.String(20))  # admin, user, superuser
    created_at = db.Column(db.DateTime)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))  # Optional contact, not login
    created_at = db.Column(db.DateTime)
```

User has email, role, and picture. Customer has code and phone. No overlap. Each table holds what makes sense for that entity.

When you order something:

```python
class Order(db.Model):
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
```

`customer_id` is who bought it. `created_by` is the staff member who processed it. Clear semantics.

## Login Flows

Staff login goes through Google OAuth:

```python
@app.route('/auth/login')
def staff_login():
    return google_client.authorize_redirect()

@app.route('/auth/authorize')
def auth_callback():
    token = google_client.get_access_token()
    user_info = google_client.get(...)
    user = User.query.filter_by(email=user_info.email).first()
    if not user:
        user = User(email=user_info.email, name=user_info.name)
        db.session.add(user)
    session['user_id'] = user.id
    session['role'] = user.role
    return redirect('/dashboard')
```

Creates or updates a User. Session established. They're logged in as staff.

Customer lookup is different:

```python
@app.route('/auth/customer-login', methods=['POST'])
def customer_login():
    code_or_email = request.json.get('code_or_email')
    customer = Customer.query.filter(
        (Customer.code == code_or_email) | (Customer.email == code_or_email)
    ).first()
    if not customer:
        return {'error': 'Customer not found'}, 404
    session['customer_id'] = customer.id
    return redirect('/shop')
```

Looks up a Customer record. Different session variable. Different flow. Customers never log in with email/password or OAuth. They use their code.

## Role-Based UI

The inventory page (checkout interface) looks different depending on who's using it:

```html
{% if user_type == 'admin' %}
  <select id="customer-selector">
    <option value="">Select customer...</option>
    {% for customer in all_customers %}
      <option value="{{ customer.id }}">{{ customer.name }}</option>
    {% endfor %}
  </select>
{% endif %}

<button id="place-order-btn" {% if user_type == 'guest' %}disabled{% endif %}>
  Place Order
</button>
```

Admin sees the dropdown. Has to pick a customer. Operator doesn't see the dropdown. Operates under their own customer account. Guest sees the button is disabled with a sign-in prompt.

Same page. Different UI based on role. All backed by API validation too.

## Why This Matters

**Reports are accurate.** Top customers query only the customers table:

```python
db.session.query(
    Customer.name,
    func.sum(Order.total_amount)
).join(Order).group_by(Customer.id)
```

No staff accounts mixed in. No pretesting data. Just actual customers.

**Permissions are clear.** Admin management is separate from customer management. `GET /api/users` returns staff. `GET /api/customers` returns buyers. No ambiguity.

**Code is straightforward.** No special case handling. No checking a role field to decide what a record means. A Customer is always a buyer. A User is always staff.

```python
# Clear and simple
def place_order():
    customer_id = request.json['customer_id']
    customer = db.session.get(Customer, customer_id)
    if not customer:
        return error('Invalid customer')
    order = Order(customer_id=customer_id, created_by=current_user.id)
```

No guessing. No defaults. No workarounds.

**Scaling is easier.** If you add new features later—loyalty points, customer tiers, account statements—they all go in the Customer table. Staff features go in User. They don't interfere.

## The Audit Trail

Because staff and customers are separate, the order audit trail is clean:

```python
class Order(db.Model):
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
```

You know exactly who created the order (which staff member) and for whom (which customer). This matters if something goes wrong. You can trace it back.

And stock movements reference the user who made the change:

```python
class StockMovement(db.Model):
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
```

Every inventory change is attributed. Who restocked this? Who cancelled an order and restored stock? All traceable.

## How It Works in Practice

Admin logs in. Sees the dashboard. Clicks Customers. Creates a new customer: name "John", code "CUST-001", phone "+254712345678".

Goes to inventory. Customer dropdown shows all customers including John. Selects John. Adds products to cart. Clicks place order.

Order created with customer_id pointing to John's record. SMS fires to that phone number. Order appears in reports under "John".

John never logs into the staff system. He never touches analytics. Staff never shows up in customer data.

Clean separation. Each party sees what they need.

## The Database Stays Consistent

Because the schema reflects reality (users are staff, customers are buyers), the code stays consistent. You don't wake up three months later wondering why a report is including phantom user accounts. The table structure prevented that from happening.

This is what good schema design does. It prevents bad code from working.

## Practical Trade-Offs

There's one place where they touch:

```python
class User(db.Model):
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
```

A staff member can optionally be linked to a customer account. If an admin is also a customer (sells products themselves), they can have both. But it's optional. The default is: users are staff, customers are buyers.

## For Your Next System

When you're building something with multiple actor types, ask: what data does each type need? What do they do? If actor A needs email login and roles, and actor B needs a phone number and SMS notifications, they're different types.

Put them in separate tables. Give them separate code paths. It takes a few extra minutes upfront. It saves hours of special-case handling later.

The schema is your contract with the codebase. Make it honest about what your entities actually are.

---

Code is on [GitHub](https://github.com/Markkimotho/make-an-order). Look at how orders relate to users and customers. That's the clarity.
