---
layout: post
title: "Building a POS System from Scratch: Lessons from Real-World Constraints"
date: 2026-03-30 10:00:00
description: How real business requirements forced architectural decisions that make the system actually useful — and why a spreadsheet mentality gets you nowhere.
tags: projects flask ecommerce pos retail backend python
categories: projects
giscus_comments: true
related_posts: true
---

I built a point-of-sale system for a reason. Not as an exercise. A real human wanted to take orders, track inventory, get notified when something sold, and know how much money they made. This is not a toy problem. It is what happens when you stop building for the internet and start building for actual business.

I started wrong. I thought: customers place orders, admin manages things. Done. Ship it.

Then reality happened.

## The First Wrong Assumption

My first implementation auto-provisioned users as customers. Every staff member who logged in got a customer record attached to them automatically. The logic was simple: when a user placed an order, link it to their auto-created customer account.

This broke almost immediately. A superuser logs in to check on things, not to buy. An admin wants to place an order on behalf of someone else, not for themselves. A warehouse operator is scanning inventory, not shopping. Yet each of them had a customer account silently created, their orders showing up in analytics, confusion spreading through the whole system.

The epiphany: **users and customers are not the same thing**.

This was not a data model problem. It was a conceptual problem. I was collapsing two completely different business entities into one table and hoping the logic would sort itself out. It would not.

## Separating Users from Customers

The fix required rethinking the whole thing:

- **User:** A staff account. Email login via Google OAuth. Role-based permissions. Can manage the platform.
- **Customer:** A buyer. Phone number. Can browse the shop and place orders. Gets SMS notifications.

These are different things with different needs. A user might manage 500 customers. A customer does not need to know they are a "user" anywhere.

I rebuilt the order flow:

**Before (broken):**
```
Staff logs in → Auto-customer created (USR-123) → Places order → Order linked to USR-123
Result: Confusion. Staff orders mixed in with real customer orders.
```

**After (correct):**
```
Admin opens inventory → Selects a customer from list → Adds items to cart → Places order on behalf of customer
OR
Customer gets sent a link → Logs in with their code/email → Browses shop → Places their own order
Result: Clean separation. No confusion.
```

The second version required:
1. A customer selector dropdown populated at runtime
2. Order placement that explicitly requires a customer_id
3. No auto-provisioning logic — admins must intentionally create or link customers
4. A completely separate customer login flow

It sounds obvious in retrospect. But you only learn this by shipping the wrong thing, watching it break, and fixing it under pressure.

## Role-Based Access Getting Complicated

Once you have separate users and customers, role-based access gets real fast.

I have three roles:

- **Superuser:** Godmode. Approve staff, link users to customers, delete anything.
- **Admin:** Run the business. Create customers, manage inventory, see reports, approve new staff.
- **User (Operator):** Work the register. Browse inventory, place orders.

But the same page (`/inventory`) serves all three roles with different permissions. An operator clicks "place order" and gets a button. An admin clicks it and gets a customer selector plus the button. A guest sees neither and gets a message: "Sign in to place orders."

This calls for conditional rendering at the template level:

```html
{% if user_role == 'admin' %}
  <select id="customer-selector"><!-- customer dropdown --></select>
{% endif %}

<button id="place-order-btn">
  {% if user_role == 'guest' %}
    (disabled - sign in to place orders)
  {% else %}
    Place Order
  {% endif %}
</button>
```

The backend validates the request too. Just because the button is hidden does not mean someone cannot forge a request. Every API call checks permissions:

```python
@admin_api_required  # Decorator checks role
def place_order():
    # Only admins can omit customer_id
    # Only non-admins auto-link to themselves
    ...
```

Double validation. Frontend hides features. Backend enforces permissions. Both are necessary.

## OAuth Getting Real

Google OAuth looked simple. Click button, redirect to Google, come back authenticated. Except it kept failing with `Error 400: redirect_uri_mismatch`.

The problem: I was dynamically constructing the redirect URL in the auth route using Flask's `url_for()` function. During local development on `localhost:5001` it worked. But when I deployed to production, the app was behind a reverse proxy, or on a different URL, and the redirect URL I was constructing did not match what Google had registered. Mismatch. Error. No login.

The fix required understanding a critical principle: **the app must know its own URL.**

```python
# .env
APP_URL="http://localhost:5001"  # Or production domain

# app.py
app_url = os.environ.get("APP_URL", "http://localhost:5001")
redirect_uri = f"{app_url}/auth/authorize"

google = oauth.register(
    redirect_uri=redirect_uri,  # Tell Authlib exactly what to use
    ...
)
```

Now the redirect URI is fixed, registered in Google Console, and matches exactly. No more mismatches. No more errors.

This taught me that you cannot rely on proxies to fix your URLs for you. The app must be explicit about where it lives.

## SMS Notifications Teaching Humility

I integrated Africa's Talking API to send SMS confirmations. Nice feature. Customer places order, SMS goes out immediately with order details.

Except what happens when the SMS API is down? Or the API key is wrong? Or the phone number format is invalid?

My first version crashed. SMS error = order endpoint error = no response to the client. Bad.

I fixed it with graceful degradation:

```python
try:
    send_sms(customer_phone, message)
except Exception as e:
    logger.warning(f"SMS failed: {str(e)}")
    # Continue anyway - order still placed
    # SMS is nice to have, not critical
```

The order goes through. The SMS either arrives or it does not. The customer does not lose their order because a third-party API had an issue at 3am.

This is a principle I should have known beforehand but learned the hard way: **dependencies are liabilities**. Minimize them. When you have to use them, isolate the failure.

## Inventory Audit Trail Saving My Bacon

I built a stock audit trail early. Every time inventory changes, log it:
- What changed (how many units)
- Why (sale, restock, cancel, refund)
- Who did it (operator ID)
- When (timestamp)

Felt like overkill at the time. Just tracking count would be simpler.

But then a customer disputed an order. "I never got my stock." I looked at the orders table and saw the sale processed. But the admin swore they refunded it. My word against theirs.

The audit trail showed the truth. Sale on date X. Refund on date Y by admin Z. There it was, immutable, timestamped. No argument.

This is a lesson learned at scale by companies that did not have it and had to rebuild. I learned it by being paranoid and building it from the start. Worth every line.

## The Guest Shop Surprise

I wanted people to browse the shop without signing in. Seemed straightforward: redirect unauthenticated users to `/shop` where they see products but cannot order.

Except the template logic was checking `@login_required`. Guest could not even see the inventory page. They hit a 403 error.

The fix: create a public `/shop` route that does not require auth, renders the same inventory template, but passes `user_role='guest'`. The template uses that variable to hide the "Place Order" button and the customer selector.

This required:
1. A new route where auth is optional
2. Conditional rendering in the template based on role
3. API validation that guests cannot call order endpoints

Again, simpler than it seemed once you separated concerns.

## What I Actually Learned

**Real requirements teach you things books cannot.** I did not start planning an audit trail. The business need for order dispute resolution forced me to think about it.

**Separation of concerns is not about purity; it is about not going insane.** Users and customers being separate meant the code I wrote for one did not accidentally affect the other.

**Double-layer validation works.** Hide the button on the frontend. Check permissions on the backend. Both must pass. One layer is a false sense of security.

**Third-party integrations need graceful fallbacks.** SMS down? Order still places. That is the only way to build systems that actually work in production.

**Audit trails are not slowness — they are your future self.** Log generously. The disk is cheap. The confusion when you need to know why something changed and the log is silent is expensive.

**Your app needs to know where it lives.** Not rely on headers or magic to figure it out. Ask it explicitly at startup. Store it in config. Use it everywhere URLs matter.

## What Comes Next

There are things I did not do yet that I will:
- Inventory forecasting based on sales trends
- Customer loyalty or points system
- Multi-location support (franchise setup)
- Advanced reporting with date range filters
- Payment gateway integration

These are not needs yet. I am not adding them. The system works for what it needs to do right now. I will let reality tell me what to build next.

That is the lesson. Not overengineering. Not overthinking. Building what is needed, shipping it, learning from where it breaks, fixing just that.

The code is on [GitHub](https://github.com/Markkimotho/make-an-order). If you are building something similar, take what is useful. The audit trail approach. The role separation. The OAuth config pattern. Leave the rest.

If you are starting from scratch and thinking you could do it simpler, go ahead. Everyone does. Come back here after your third redesign. I will still be here.
