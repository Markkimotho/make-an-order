"""Billing: invoices, payment requests, confirmed payments, allocations.

Only services/billing writes these tables. The money model:

  Invoice           what is owed. balance = total - amount_adjusted - amount_paid
  InvoiceAdjustment a credit note lowering an invoice (returns, corrections)
  PaymentIntent     an STK push we asked Safaricom to send; a request, not money
  Payment           money actually received, one row per M-Pesa receipt
  PaymentAllocation which invoice a payment (or part of it) pays; voided rows
                    are kept as history when money is moved or reversed

amount_paid and amount_adjusted are stored for fast aging queries but are
always recomputed from live allocations and adjustments under a row lock.
"""
import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import validates

from models.base import TenantMixin, db
from services.phone import normalize_ke_phone

INVOICE_KINDS = ("sale", "credit", "opening_balance")
INVOICE_STATUSES = ("open", "partial", "paid", "void", "expired")
INTENT_STATUSES = ("initiating", "pending", "callback_received", "completed", "failed",
                   "timeout")
PAYMENT_METHODS = ("mpesa_stk", "mpesa_c2b", "mpesa_manual", "cash", "card")
MPESA_METHODS = ("mpesa_stk", "mpesa_c2b", "mpesa_manual")
PAYMENT_STATUSES = ("confirmed", "reversed")
MATCH_STATUSES = ("matched", "partial", "unmatched")
ADJUSTMENT_REASONS = ("return", "rejected_delivery", "void", "correction")

ZERO = Decimal("0.00")


def _token():
    return secrets.token_urlsafe(18)


class Invoice(TenantMixin, db.Model):
    __tablename__ = "invoices"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "invoice_number", name="uq_invoices_tenant_invoice_number"),
        db.UniqueConstraint("tenant_id", "pay_token", name="uq_invoices_tenant_pay_token"),
    )

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    invoice_number  = db.Column(db.String(12), nullable=False)
    kind            = db.Column(db.String(20), nullable=False, default="sale")
    customer_id     = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"),
                                nullable=True, index=True)
    transaction_id  = db.Column(db.Integer, db.ForeignKey("transactions.id", ondelete="SET NULL"),
                                nullable=True, index=True)
    order_id        = db.Column(db.Integer, db.ForeignKey("orders.id", ondelete="SET NULL"),
                                nullable=True, index=True)
    total           = db.Column(db.Numeric(12, 2), nullable=False)
    amount_paid     = db.Column(db.Numeric(12, 2), nullable=False, default=ZERO)
    amount_adjusted = db.Column(db.Numeric(12, 2), nullable=False, default=ZERO)
    status          = db.Column(db.String(10), nullable=False, default="open")
    issued_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    due_date        = db.Column(db.Date, nullable=True)
    pay_token       = db.Column(db.String(32), nullable=False, default=_token)
    etims_status    = db.Column(db.String(20), nullable=False, default="not_submitted")
    etims_cuin      = db.Column(db.String(64), nullable=True)
    etims_qr_url    = db.Column(db.String(255), nullable=True)
    external_etims_ref = db.Column(db.String(64), nullable=True)
    notes           = db.Column(db.Text, nullable=True)

    allocations = db.relationship("PaymentAllocation", backref="invoice", lazy=True)
    adjustments = db.relationship("InvoiceAdjustment", backref="invoice", lazy=True)

    @property
    def balance(self):
        """What is still owed. A void invoice owes nothing."""
        if self.status == "void":
            return ZERO
        return (Decimal(self.total or 0) - Decimal(self.amount_adjusted or 0)
                - Decimal(self.amount_paid or 0))

    def to_dict(self):
        return {
            "id": self.id,
            "invoice_number": self.invoice_number,
            "kind": self.kind,
            "customer_id": self.customer_id,
            "transaction_id": self.transaction_id,
            "order_id": self.order_id,
            "total": float(self.total),
            "amount_paid": float(self.amount_paid or 0),
            "amount_adjusted": float(self.amount_adjusted or 0),
            "balance": float(self.balance),
            "status": self.status,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "etims_status": self.etims_status,
            "external_etims_ref": self.external_etims_ref,
        }


class PaymentIntent(TenantMixin, db.Model):
    """One STK push. Written 'initiating' before Daraja is called."""
    __tablename__ = "payment_intents"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "checkout_request_id",
                            name="uq_payment_intents_tenant_checkout_request_id"),
    )

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    invoice_id          = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"),
                                    nullable=False, index=True)
    checkout_request_id = db.Column(db.String(80), nullable=True)
    merchant_request_id = db.Column(db.String(80), nullable=True)
    phone               = db.Column(db.String(15), nullable=False)
    amount              = db.Column(db.Numeric(12, 2), nullable=False)
    status              = db.Column(db.String(20), nullable=False, default="initiating")
    result_desc         = db.Column(db.String(255), nullable=True)
    raw_callback        = db.Column(db.JSON, nullable=True)
    payment_id          = db.Column(db.Integer, db.ForeignKey("payments.id", ondelete="SET NULL"),
                                    nullable=True)
    initiated_by        = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                                    nullable=True)
    last_queried_at     = db.Column(db.DateTime, nullable=True)
    created_at          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    invoice = db.relationship("Invoice")

    @validates("phone")
    def _normalize_phone(self, key, value):
        return normalize_ke_phone(value)

    def to_dict(self):
        return {
            "id": self.id,
            "invoice_id": self.invoice_id,
            "checkout_request_id": self.checkout_request_id,
            "phone": self.phone,
            "amount": float(self.amount),
            "status": self.status,
            "result_desc": self.result_desc,
            "payment_id": self.payment_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Payment(TenantMixin, db.Model):
    """Money received. One row per M-Pesa receipt, whichever way it arrived."""
    __tablename__ = "payments"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "mpesa_receipt", name="uq_payments_tenant_mpesa_receipt"),
    )

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    method              = db.Column(db.String(20), nullable=False)
    amount              = db.Column(db.Numeric(12, 2), nullable=False)
    unallocated_amount  = db.Column(db.Numeric(12, 2), nullable=False, default=ZERO)
    status              = db.Column(db.String(10), nullable=False, default="confirmed")
    match_status        = db.Column(db.String(10), nullable=False, default="unmatched")
    verified            = db.Column(db.Boolean, nullable=False, default=True)
    claimed_amount      = db.Column(db.Numeric(12, 2), nullable=True)
    mpesa_receipt       = db.Column(db.String(32), nullable=True)
    bill_ref_raw        = db.Column(db.String(64), nullable=True)
    payer_phone         = db.Column(db.String(64), nullable=True)   # C2B sends it masked/hashed
    payer_name          = db.Column(db.String(255), nullable=True)
    customer_id         = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"),
                                    nullable=True, index=True)
    org_account_balance = db.Column(db.Numeric(14, 2), nullable=True)
    recorded_by         = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                                    nullable=True)
    raw_payload         = db.Column(db.JSON, nullable=True)
    received_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    allocations = db.relationship("PaymentAllocation", backref="payment", lazy=True)

    @validates("mpesa_receipt")
    def _normalize_receipt(self, key, value):
        value = (value or "").strip().upper()
        return value or None

    def to_dict(self):
        return {
            "id": self.id,
            "method": self.method,
            "amount": float(self.amount),
            "unallocated_amount": float(self.unallocated_amount or 0),
            "status": self.status,
            "match_status": self.match_status,
            "verified": bool(self.verified),
            "claimed_amount": float(self.claimed_amount) if self.claimed_amount is not None else None,
            "mpesa_receipt": self.mpesa_receipt,
            "bill_ref": self.bill_ref_raw,
            "payer_name": self.payer_name,
            "customer_id": self.customer_id,
            "recorded_by": self.recorded_by,
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }


class PaymentAllocation(TenantMixin, db.Model):
    __tablename__ = "payment_allocations"

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    payment_id  = db.Column(db.Integer, db.ForeignKey("payments.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    invoice_id  = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    amount      = db.Column(db.Numeric(12, 2), nullable=False)
    voided_at   = db.Column(db.DateTime, nullable=True)
    voided_by   = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "payment_id": self.payment_id, "invoice_id": self.invoice_id,
                "amount": float(self.amount),
                "voided_at": self.voided_at.isoformat() if self.voided_at else None}


class InvoiceAdjustment(TenantMixin, db.Model):
    __tablename__ = "invoice_adjustments"

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    invoice_id  = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    amount      = db.Column(db.Numeric(12, 2), nullable=False)
    reason      = db.Column(db.String(20), nullable=False)
    note        = db.Column(db.String(255), nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "invoice_id": self.invoice_id, "amount": float(self.amount),
                "reason": self.reason, "note": self.note,
                "created_at": self.created_at.isoformat() if self.created_at else None}
