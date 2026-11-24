"""Billing service: invoices, payments, allocation, credit notes, reversals."""
from decimal import Decimal

import pytest

from auth.tenancy import tenant_scope
from models import AuditLog, Customer, Invoice, Payment, PaymentAllocation, db
from services import billing
from services.billing import BillingError, DuplicateReceipt

D = Decimal


@pytest.fixture
def duka(app):
    c = Customer(name="Mama Mboga Duka", phone_number="0712000111", code="DUKA1",
                 credit_limit=D("10000"))
    db.session.add(c)
    db.session.commit()
    return c


def inv(total, customer=None, kind="credit"):
    invoice = billing.create_invoice(total=total, kind=kind,
                                     customer_id=customer.id if customer else None)
    db.session.commit()
    return invoice


def state(invoice):
    invoice = db.session.get(Invoice, invoice.id)
    return invoice.status, invoice.balance


def pay(amount, **kw):
    p = billing.record_payment(method=kw.pop("method", "mpesa_c2b"), amount=amount, **kw)
    db.session.commit()
    return p


# --- invoices -----------------------------------------------------------------

def test_invoice_numbers_are_sequential_per_business(app, other_tenant):
    assert inv(10).invoice_number == "INV000001"
    assert inv(10).invoice_number == "INV000002"
    with tenant_scope(other_tenant):
        assert inv(10).invoice_number == "INV000001"


def test_invoice_total_must_be_positive(app):
    with pytest.raises(BillingError):
        billing.create_invoice(total=0)


# --- allocation -----------------------------------------------------------------

def test_exact_payment(duka):
    i = inv(1000, duka)
    p = pay(1000, receipt="SJK0000001", bill_ref=i.invoice_number)
    assert state(i) == ("paid", D("0.00"))
    assert (p.match_status, p.unallocated_amount) == ("matched", D("0.00"))
    assert p.customer_id == duka.id


def test_partial_then_remainder(duka):
    i = inv(1000, duka)
    pay(400, receipt="SJK0000001", bill_ref=i.invoice_number)
    assert state(i) == ("partial", D("600.00"))
    pay(600, receipt="SJK0000002", bill_ref=i.invoice_number)
    assert state(i) == ("paid", D("0.00"))


def test_overpay_rolls_to_next_invoice_then_credit(duka):
    first, second = inv(1000, duka), inv(500, duka)
    p = pay(1800, receipt="SJK0000001", bill_ref=first.invoice_number)
    assert state(first) == ("paid", D("0.00"))
    assert state(second) == ("paid", D("0.00"))
    assert (p.match_status, p.unallocated_amount) == ("partial", D("300.00"))
    assert billing.customer_exposure(duka.id) == D("-300.00")


def test_payer_invoices_are_paid_oldest_first(duka):
    old, new = inv(300, duka), inv(300, duka)
    target = inv(100, duka)
    pay(450, receipt="SJK0000001", bill_ref=target.invoice_number)
    assert state(target)[0] == "paid"
    assert state(old) == ("paid", D("0.00"))
    assert state(new) == ("partial", D("250.00"))


def test_reference_matching_ignores_case_and_spaces(duka):
    i = inv(100, duka)
    pay(100, receipt="SJK0000001", bill_ref=" inv 000001 ")
    assert state(i)[0] == "paid"


def test_unknown_reference_goes_to_review(app):
    p = pay(250, receipt="SJK0000001", bill_ref="WHO-KNOWS")
    assert (p.match_status, p.unallocated_amount) == ("unmatched", D("250.00"))
    assert [r["mpesa_receipt"] for r in billing.review_queue()["unmatched"]] == ["SJK0000001"]


# --- one receipt, many arrivals ------------------------------------------------------

@pytest.mark.parametrize("order", [("mpesa_stk", "mpesa_c2b"), ("mpesa_c2b", "mpesa_stk")])
def test_same_receipt_twice_is_one_payment(duka, order):
    i = inv(500, duka)
    for method in order:
        pay(500, method=method, receipt="SJK0000001", invoice=i)
    assert Payment.query.count() == 1
    assert PaymentAllocation.query.filter(PaymentAllocation.voided_at.is_(None)).count() == 1
    assert state(i) == ("paid", D("0.00"))


def test_receipt_is_case_insensitive(duka):
    i = inv(500, duka)
    pay(500, method="mpesa_manual", receipt="sjk0000001", invoice=i, verified=False)
    pay(500, method="mpesa_c2b", receipt="SJK0000001")
    p = Payment.query.one()
    assert p.verified and p.mpesa_receipt == "SJK0000001"


def test_typed_code_twice_is_refused(duka):
    i = inv(500, duka)
    pay(500, method="mpesa_manual", receipt="SJK0000001", invoice=i, verified=False)
    with pytest.raises(DuplicateReceipt):
        billing.record_payment(method="mpesa_manual", amount=500, receipt="SJK0000001",
                               invoice=i, verified=False)


def test_typed_amount_wrong_daraja_wins(duka):
    i = inv(5000, duka)
    pay(5000, method="mpesa_manual", receipt="SJK0000001", invoice=i, verified=False,
        recorded_by=None)
    assert state(i)[0] == "paid"
    pay(500, method="mpesa_c2b", receipt="SJK0000001")
    p = Payment.query.one()
    assert (p.amount, p.claimed_amount, p.verified) == (D("500.00"), D("5000.00"), True)
    assert state(i) == ("partial", D("4500.00"))
    assert AuditLog.query.filter_by(action="payment.amount_mismatch").count() == 1
    assert [m["mpesa_receipt"] for m in billing.review_queue()["amount_mismatches"]] == [
        "SJK0000001"]


def test_till_payment_first_unmatched_then_stk_matches_it(duka):
    """Till C2B carries no account reference; the STK callback for the same
    receipt then names the invoice."""
    i = inv(300, duka)
    p = pay(300, method="mpesa_c2b", receipt="SJK0000001")
    assert p.match_status == "unmatched"
    pay(300, method="mpesa_stk", receipt="SJK0000001", invoice=i)
    assert state(i)[0] == "paid"
    assert Payment.query.one().match_status == "matched"


# --- credit notes, voids, moves, reversals ---------------------------------------------

def test_credit_note_after_full_payment_becomes_credit_and_rolls(duka):
    paid_invoice = inv(1000, duka)
    pay(1000, receipt="SJK0000001", bill_ref=paid_invoice.invoice_number)
    later = inv(150, duka)
    billing.adjust(paid_invoice, 200, "return", note="2 crates damaged")
    db.session.commit()
    assert state(paid_invoice) == ("paid", D("0.00"))
    assert db.session.get(Invoice, paid_invoice.id).amount_adjusted == D("200.00")
    assert state(later) == ("paid", D("0.00"))
    assert Payment.query.one().unallocated_amount == D("50.00")


def test_credit_note_cannot_exceed_invoice(duka):
    i = inv(100, duka)
    with pytest.raises(BillingError):
        billing.adjust(i, 150, "correction")


def test_void_paid_invoice_money_becomes_credit(duka):
    i = inv(400, duka)
    pay(400, receipt="SJK0000001", bill_ref=i.invoice_number)
    billing.void_invoice(i)
    db.session.commit()
    assert state(i)[0] == "void"
    p = Payment.query.one()
    assert (p.unallocated_amount, p.match_status) == (D("400.00"), "unmatched")


def test_reallocate_wrong_account(duka, app):
    other = Customer(name="Other", phone_number="0712000222", code="OTH")
    db.session.add(other)
    db.session.commit()
    wrong, right = inv(500, duka), inv(500, other)
    p = pay(500, receipt="SJK0000001", bill_ref=wrong.invoice_number)
    billing.reallocate(p, right)
    db.session.commit()
    assert state(wrong) == ("open", D("500.00"))
    assert state(right) == ("paid", D("0.00"))
    assert p.customer_id == other.id


def test_reversal_reopens_invoice(duka):
    i = inv(500, duka)
    p = pay(500, receipt="SJK0000001", bill_ref=i.invoice_number)
    billing.reverse_payment(p, reason="Safaricom reversal")
    db.session.commit()
    assert state(i) == ("open", D("500.00"))
    assert (p.status, p.unallocated_amount) == ("reversed", D("0"))


def test_expired_invoice_paid_late_reopens(duka):
    i = inv(200, duka)
    i.status = "expired"
    db.session.commit()
    pay(200, receipt="SJK0000001", bill_ref=i.invoice_number)
    assert state(i) == ("paid", D("0.00"))


def test_expired_invoice_not_paid_by_rolling_money(duka):
    expired = inv(200, duka)
    expired.status = "expired"
    db.session.commit()
    target = inv(100, duka)
    p = pay(300, receipt="SJK0000001", bill_ref=target.invoice_number)
    assert state(expired) == ("expired", D("200.00"))
    assert p.unallocated_amount == D("200.00")


# --- exposure, review ------------------------------------------------------------------

def test_exposure_counts_credit_invoices_less_unapplied_money(duka):
    inv(1000, duka)
    inv(500, duka, kind="sale")        # a pay-now sale is not credit exposure
    assert billing.customer_exposure(duka.id) == D("1000.00")
    pay(200, method="cash", customer_id=duka.id)
    assert billing.customer_exposure(duka.id) == D("800.00")


def test_possible_missed_payment_hint(app):
    pay(100, receipt="SJK0000001", bill_ref="X", org_account_balance=D("10100"))
    pay(50, receipt="SJK0000002", bill_ref="X", org_account_balance=D("10150"))
    assert billing.possible_missed_payments() == []
    pay(30, receipt="SJK0000003", bill_ref="X", org_account_balance=D("10480"))
    hints = billing.possible_missed_payments()
    assert len(hints) == 1 and hints[0]["unexplained_amount"] == 300.0


def test_withdrawal_is_not_a_missed_payment(app):
    pay(100, receipt="SJK0000001", bill_ref="X", org_account_balance=D("10100"))
    pay(50, receipt="SJK0000002", bill_ref="X", org_account_balance=D("2150"))
    assert billing.possible_missed_payments() == []


def test_money_rounds_to_cents():
    assert billing.money("100.005") == D("100.01")
    assert billing.money(None) == D("0.00")
