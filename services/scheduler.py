"""Daily SMS triggers, run once a day by ``flask run-daily-triggers``.

For every business whose subscription is active (or in trial), in its own
transaction so one failure never stops the rest:

* Refill reminders: active chronic prescriptions whose refill is due in
  ``REFILL_LEAD_DAYS`` days (day 26 of a 30-day course) get an SMS with the
  refill link. A prescription with a controlled drug is told to call or
  visit instead: its refill cannot be ordered from a link.
* Overdue credit reminders: credit invoices with a balance past their due
  date, at 1, 3, 7 and 14 days, then once a week. A text every day gets
  the sender blocked.

Every message has a dedupe key carrying its stage (``refill:<rx>:<due>``,
``overdue:<invoice>:d3``, ``overdue:<invoice>:w2`` for the second week), so a rerun the
same day sends nothing and a missed day catches up with the latest stage
reached, once. "Today" is Nairobi's date (services/clock.py): the server
runs on UTC, three hours behind.

Links need PUBLIC_BASE_URL (a text message cannot carry a relative link).
"""
import logging
from datetime import timedelta

from flask import current_app

from auth.tenancy import tenant_scope, unscoped
from models import Customer, Invoice, Prescription, Tenant, db
from services import billing, sms_service
from services.billing.invoices import money
from services.clock import today_nairobi

logger = logging.getLogger(__name__)

REFILL_LEAD_DAYS = 4
OVERDUE_STAGES = (1, 3, 7, 14)          # then weekly


class SchedulerNotConfigured(RuntimeError):
    pass


def overdue_stage(days_past, today):
    """The dedupe stage for an invoice ``days_past`` days overdue, or None
    when it is not yet due a reminder."""
    if days_past < OVERDUE_STAGES[0]:
        return None
    if days_past < 21:
        reached = [s for s in OVERDUE_STAGES if s <= days_past]
        return f"d{reached[-1]}"
    # Weekly from day 21, counted from the due date (not calendar weeks,
    # which would put two reminders six days apart across a Monday).
    return f"w{(days_past - 21) // 7 + 1}"


def _base_url():
    base = current_app.config.get("PUBLIC_BASE_URL")
    if not base:
        raise SchedulerNotConfigured("PUBLIC_BASE_URL is not set; reminders need absolute links")
    return base.rstrip("/")


def refill_reminders(tenant, today):
    """Text patients whose refill is due in REFILL_LEAD_DAYS days."""
    from verticals.pharmacy import service as pharmacy
    due = today + timedelta(days=REFILL_LEAD_DAYS)
    sent = 0
    rows = (Prescription.query
            .filter(Prescription.is_chronic.is_(True), Prescription.refill_status == "active",
                    Prescription.refill_due_date == due)
            .order_by(Prescription.id).all())
    for rx in rows:
        customer = db.session.get(Customer, rx.customer_id)
        if customer is None or not customer.phone_number or not rx.refill_token:
            continue
        first = (customer.name or "").split()[0] if customer.name else "Hello"
        if pharmacy.has_controlled(rx):
            body = (f"{first}, your refill at {tenant.name} is due on {due:%d %b}. "
                    "Please call or visit the pharmacy to collect it.")
        else:
            body = (f"{first}, your refill at {tenant.name} is due on {due:%d %b}. "
                    f"Order and pay here: {_base_url()}/o/{tenant.slug}/r/{rx.refill_token}")
        db.session.commit()                       # send() commits; nothing may be pending
        message = sms_service.send(customer.phone_number, body, "refill_reminder",
                                   dedupe_key=f"refill:{rx.id}:{due.isoformat()}")
        sent += message.status == "sent"
    return sent


def overdue_reminders(tenant, today):
    """Text customers about credit invoices past due, at the stage reached."""
    sent = 0
    invoices = (Invoice.query
                .filter(Invoice.kind.in_(billing.CREDIT_KINDS),
                        Invoice.status.in_(("open", "partial")),
                        Invoice.due_date.isnot(None), Invoice.due_date < today,
                        Invoice.customer_id.isnot(None))
                .order_by(Invoice.due_date, Invoice.id).all())
    for invoice in invoices:
        balance = money(invoice.balance)
        stage = overdue_stage((today - invoice.due_date).days, today)
        if balance <= 0 or stage is None:
            continue
        customer = db.session.get(Customer, invoice.customer_id)
        if customer is None or not customer.phone_number:
            continue
        link = f"{_base_url()}/o/{tenant.slug}/pay/{invoice.pay_token}"
        body = (f"{customer.name}: invoice {invoice.invoice_number} from {tenant.name} has "
                f"KES {balance:,.2f} overdue since {invoice.due_date:%d %b}. "
                f"Pay by M-Pesa: {link}")
        db.session.commit()
        message = sms_service.send(customer.phone_number, body, "overdue_reminder",
                                   dedupe_key=f"overdue:{invoice.id}:{stage}")
        sent += message.status == "sent"
    return sent


def _eligible_tenants():
    """Businesses that get reminders: trial or active, not lapsed."""
    with unscoped():
        tenants = Tenant.query.order_by(Tenant.id).all()
        return [(t.id, t.slug) for t in tenants if t.is_active_subscription()]


def run_daily_triggers(today=None):
    """Run every trigger for every eligible business. Returns
    ``{slug: {"refills": n, "overdue": n} | {"error": "..."}}``."""
    _base_url()                                   # fail once, early, if links cannot be built
    today = today or today_nairobi()
    results = {}
    for tenant_id, slug in _eligible_tenants():
        try:
            with tenant_scope(tenant_id):
                tenant = db.session.get(Tenant, tenant_id)
                counts = {"refills": refill_reminders(tenant, today),
                          "overdue": overdue_reminders(tenant, today)}
                db.session.commit()
            results[slug] = counts
            logger.info("daily triggers %s %s: %s", today.isoformat(), slug, counts)
        except Exception as e:                     # one business never stops the rest
            db.session.rollback()
            results[slug] = {"error": str(e)[:200]}
            logger.error("daily triggers %s %s failed: %s", today.isoformat(), slug, e,
                         exc_info=True)
    return results
