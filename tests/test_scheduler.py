"""Daily triggers: refill and overdue reminders, stages and dedupe, the
Nairobi date, one business failing, lapsed businesses, the CLI."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope, unscoped
from models import (Customer, InventoryItem, Prescription, PrescriptionItem, SmsMessage, Tenant,
                    db)
from services import billing, scheduler
from services.clock import today_nairobi

D = Decimal
TODAY = date(2026, 12, 1)


@pytest.fixture
def base_url(app, monkeypatch):
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", "https://pay.example.co.ke")


def customer(name="Wanjiku Kamau", phone="0722000111", code="WK"):
    c = Customer(name=name, phone_number=phone, code=code, credit_limit=D("100000"))
    db.session.add(c)
    db.session.flush()
    return c


def chronic(cust, due, controlled=False, status="active"):
    item = InventoryItem(name="Morphine" if controlled else "Metformin", price=D("100"),
                         is_controlled=controlled)
    db.session.add(item)
    db.session.flush()
    rx = Prescription(customer_id=cust.id, is_chronic=True, duration_days=30,
                      refill_due_date=due, refill_status=status,
                      refill_token=f"tok{cust.id}{int(controlled)}{status}")
    db.session.add(rx)
    db.session.flush()
    db.session.add(PrescriptionItem(prescription_id=rx.id, inventory_item_id=item.id,
                                    quantity_prescribed=D("30")))
    db.session.commit()
    return rx


def messages(kind=None):
    q = SmsMessage.query
    if kind:
        q = q.filter_by(kind=kind)
    return q.order_by(SmsMessage.id).all()


# --- stages ------------------------------------------------------------------------

@pytest.mark.parametrize("days,stage", [(0, None), (1, "d1"), (2, "d1"), (3, "d3"), (6, "d3"),
                                        (7, "d7"), (13, "d7"), (14, "d14"), (20, "d14"),
                                        (21, "w1"), (27, "w1"), (28, "w2"), (40, "w3")])
def test_overdue_stage(days, stage):
    assert scheduler.overdue_stage(days, TODAY) == stage


def test_today_is_nairobis_date_at_2230_utc():
    assert today_nairobi(datetime(2026, 11, 30, 22, 30)) == TODAY


# --- refill reminders -------------------------------------------------------------------

def test_refill_reminder_four_days_ahead_with_the_link(app, base_url, sms_outbox):
    wanjiku = customer()
    token = chronic(wanjiku, TODAY + timedelta(days=4)).refill_token
    chronic(customer("Late One", "0722000112", "L1"), TODAY + timedelta(days=5))
    chronic(customer("Paused", "0722000113", "P1"), TODAY + timedelta(days=4), status="paused")
    results = scheduler.run_daily_triggers(TODAY)
    assert results["acme"] == {"refills": 1, "overdue": 0}
    assert len(sms_outbox.sent) == 1
    text = sms_outbox.sent[0]["message"]
    assert text.startswith("Wanjiku,") and "05 Dec" in text
    assert f"https://pay.example.co.ke/o/acme/r/{token}" in text
    scheduler.run_daily_triggers(TODAY)
    assert len(sms_outbox.sent) == 1                               # rerun sends nothing


def test_controlled_refills_are_told_to_call(app, base_url, sms_outbox):
    chronic(customer(), TODAY + timedelta(days=4), controlled=True)
    scheduler.run_daily_triggers(TODAY)
    text = sms_outbox.sent[0]["message"]
    assert "call or visit" in text and "/r/" not in text


# --- overdue reminders ----------------------------------------------------------------------

def test_overdue_reminders_follow_the_stages_once_each(app, base_url, sms_outbox):
    duka = customer("Mama Mboga", "0712000777", "MM")
    invoice = billing.create_invoice(total=D("1500"), kind="credit", customer_id=duka.id,
                                     due_date=TODAY)
    number, token = invoice.invoice_number, invoice.pay_token
    db.session.commit()
    sent_on = []
    for offset in range(0, 30):
        before = len(sms_outbox.sent)
        scheduler.run_daily_triggers(TODAY + timedelta(days=offset))
        if len(sms_outbox.sent) > before:
            sent_on.append(offset)
    assert sent_on == [1, 3, 7, 14, 21, 28]                       # then every 7 days
    text = sms_outbox.sent[0]["message"]
    assert number in text and "1,500.00" in text
    assert f"/o/acme/pay/{token}" in text


def test_a_missed_day_catches_up_once(app, base_url, sms_outbox):
    duka = customer("Mama Mboga", "0712000777", "MM")
    billing.create_invoice(total=D("500"), kind="credit", customer_id=duka.id,
                           due_date=TODAY - timedelta(days=10))
    db.session.commit()
    scheduler.run_daily_triggers(TODAY)
    scheduler.run_daily_triggers(TODAY + timedelta(days=1))
    assert [m.dedupe_key.rsplit(":", 1)[1] for m in messages("overdue_reminder")] == ["d7"]


def test_paid_pay_now_and_expired_invoices_get_no_reminder(app, base_url, sms_outbox):
    duka = customer("Mama Mboga", "0712000777", "MM")
    paid = billing.create_invoice(total=D("100"), kind="credit", customer_id=duka.id,
                                  due_date=TODAY - timedelta(days=3))
    billing.create_invoice(total=D("100"), kind="sale", customer_id=duka.id)
    expired = billing.create_invoice(total=D("100"), kind="credit", customer_id=duka.id,
                                     due_date=TODAY - timedelta(days=3))
    expired.status = "expired"
    db.session.flush()
    billing.record_payment(method="cash", amount=D("100"), invoice=paid)
    db.session.commit()
    assert scheduler.run_daily_triggers(TODAY)["acme"]["overdue"] == 0
    assert sms_outbox.sent == []


# --- businesses ---------------------------------------------------------------------------------

def test_one_business_failing_does_not_stop_the_others(app, base_url, other_tenant, sms_outbox,
                                                      monkeypatch):
    with tenant_scope(other_tenant):
        duka = customer("Globex Duka", "0712000888", "GD")
        billing.create_invoice(total=D("100"), kind="credit", customer_id=duka.id,
                               due_date=TODAY - timedelta(days=1))
        db.session.commit()
    real = scheduler.refill_reminders

    def broken(tenant, today):
        if tenant.slug == "acme":
            raise RuntimeError("acme is broken")
        return real(tenant, today)

    monkeypatch.setattr(scheduler, "refill_reminders", broken)
    results = scheduler.run_daily_triggers(TODAY)
    assert results["acme"] == {"error": "acme is broken"}
    assert results["globex"] == {"refills": 0, "overdue": 1}


def test_lapsed_businesses_get_no_reminders(app, base_url, sms_outbox):
    duka = customer("Mama Mboga", "0712000777", "MM")
    billing.create_invoice(total=D("100"), kind="credit", customer_id=duka.id,
                           due_date=TODAY - timedelta(days=1))
    db.session.commit()
    with unscoped():
        db.session.get(Tenant, app.acme_id).subscription_status = "cancelled"
        db.session.commit()
    assert "acme" not in scheduler.run_daily_triggers(TODAY)
    assert sms_outbox.sent == []


def test_links_need_a_public_base_url(app, monkeypatch):
    monkeypatch.setitem(app.config, "PUBLIC_BASE_URL", None)
    with pytest.raises(scheduler.SchedulerNotConfigured):
        scheduler.run_daily_triggers(TODAY)
    result = app.test_cli_runner().invoke(args=["run-daily-triggers"])
    assert result.exit_code != 0 and "PUBLIC_BASE_URL" in result.output


def test_cli(app, base_url, sms_outbox):
    chronic(customer(), TODAY + timedelta(days=4))
    result = app.test_cli_runner().invoke(args=["run-daily-triggers", "--date", TODAY.isoformat()])
    assert result.exit_code == 0, result.output
    assert "acme: {'refills': 1, 'overdue': 0}" in result.output
    assert app.test_cli_runner().invoke(args=["run-daily-triggers", "--date", "1 Dec"]).exit_code != 0
