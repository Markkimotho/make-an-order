"""Runs tests/evals/reconciliation_cases.json against the billing service.

Adding a case to the JSON adds a test; no code change needed.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from models import Customer, Invoice, Payment, db
from services import billing

CASES = json.loads((Path(__file__).parent / "evals" / "reconciliation_cases.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_case(app, case):
    customers = {}
    for i, key in enumerate(CASES["customers"]):
        c = Customer(name=key, phone_number=f"07120005{i:02d}", code=key.upper())
        db.session.add(c)
        db.session.flush()
        customers[key] = c.id
    invoices = {}
    for key, spec in case["invoices"].items():
        invoices[key] = billing.create_invoice(
            total=spec["total"], kind="credit" if spec.get("customer") else "sale",
            customer_id=customers.get(spec.get("customer")))
    db.session.commit()

    def resolve(ref):
        return invoices[ref].invoice_number if ref in invoices else ref

    by_receipt = {}
    for n, step in enumerate(case["steps"]):
        (action, args), = step.items()
        if action == "pay":
            receipt = args.get("receipt")
            method = args.get("method", "mpesa_c2b")
            ref = args.get("ref")
            kwargs = {"invoice": invoices[ref]} if method == "cash" and ref in invoices else \
                {"bill_ref": resolve(ref)} if ref else {}
            p = billing.record_payment(method=method, amount=args["amount"],
                                       receipt=receipt if method != "cash" else None,
                                       customer_id=customers.get(args.get("customer")), **kwargs)
            by_receipt[receipt or f"step{n}"] = p
        elif action == "adjust":
            billing.adjust(invoices[args["invoice"]], args["amount"], args["reason"])
        elif action == "void":
            billing.void_invoice(invoices[args["invoice"]])
        elif action == "reverse":
            billing.reverse_payment(by_receipt[args["receipt"]])
        elif action == "reassign":
            billing.reallocate(by_receipt[args["receipt"]], invoices[args["invoice"]])
        else:
            raise AssertionError(f"unknown step {action}")
        db.session.commit()

    expect = case["expect"]
    for key, (status, balance) in expect.get("invoices", {}).items():
        invoice = db.session.get(Invoice, invoices[key].id)
        assert (invoice.status, invoice.balance) == (status, Decimal(str(balance)).quantize(
            Decimal("0.01"))), f"invoice {key}"
    for receipt, (match, unallocated) in expect.get("payments", {}).items():
        payment = db.session.get(Payment, by_receipt[receipt].id)
        assert (payment.match_status, payment.unallocated_amount) == (
            match, Decimal(str(unallocated)).quantize(Decimal("0.01"))), f"payment {receipt}"
    if "payment_count" in expect:
        assert Payment.query.count() == expect["payment_count"]
