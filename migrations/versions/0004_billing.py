"""Billing: invoices, payment intents, payments, allocations, credit notes.

Every customer with a stored credit_balance gets one opening-balance
invoice (due today) for that amount, numbered from INV000001 within its
business, and then customers.credit_balance is dropped: what a customer
owes is from now on the sum of their open invoices.

Also adds customers.credit_term_days (default 30) and
tenants.manual_mpesa_enabled (default on).

Downgrade puts each customer's open invoice balance back into
credit_balance. It refuses when any invoice other than an opening balance
has payments, because that history cannot be squeezed back into one number.

Revision ID: 0004_billing
Revises: 0003_tenants_contract
Create Date: 2026-11-24
"""
import secrets
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision = "0004_billing"
down_revision = "0003_tenants_contract"
branch_labels = None
depends_on = None


def _tenant_fk(table):
    return sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                   name=f"fk_{table}_tenant_id_tenants", ondelete="CASCADE")


def upgrade():
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("invoice_number", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("amount_adjusted", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("pay_token", sa.String(length=32), nullable=False),
        sa.Column("etims_status", sa.String(length=20), nullable=False),
        sa.Column("etims_cuin", sa.String(length=64), nullable=True),
        sa.Column("etims_qr_url", sa.String(length=255), nullable=True),
        sa.Column("external_etims_ref", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_invoices_customer_id_customers", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_invoices_order_id_orders", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"],
                                name="fk_invoices_transaction_id_transactions",
                                ondelete="SET NULL"),
        _tenant_fk("invoices"),
        sa.PrimaryKeyConstraint("id", name="pk_invoices"),
        sa.UniqueConstraint("tenant_id", "invoice_number",
                            name="uq_invoices_tenant_invoice_number"),
        sa.UniqueConstraint("tenant_id", "pay_token", name="uq_invoices_tenant_pay_token"),
    )
    for col in ("tenant_id", "customer_id", "transaction_id", "order_id"):
        op.create_index(f"ix_invoices_{col}", "invoices", [col])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("unallocated_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("match_status", sa.String(length=10), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("claimed_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("mpesa_receipt", sa.String(length=32), nullable=True),
        sa.Column("bill_ref_raw", sa.String(length=64), nullable=True),
        sa.Column("payer_phone", sa.String(length=64), nullable=True),
        sa.Column("payer_name", sa.String(length=255), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("org_account_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("recorded_by", sa.Integer(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_payments_customer_id_customers", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"],
                                name="fk_payments_recorded_by_users", ondelete="SET NULL"),
        _tenant_fk("payments"),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint("tenant_id", "mpesa_receipt",
                            name="uq_payments_tenant_mpesa_receipt"),
    )
    op.create_index("ix_payments_tenant_id", "payments", ["tenant_id"])
    op.create_index("ix_payments_customer_id", "payments", ["customer_id"])

    op.create_table(
        "payment_intents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("checkout_request_id", sa.String(length=80), nullable=True),
        sa.Column("merchant_request_id", sa.String(length=80), nullable=True),
        sa.Column("phone", sa.String(length=15), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_desc", sa.String(length=255), nullable=True),
        sa.Column("raw_callback", sa.JSON(), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("initiated_by", sa.Integer(), nullable=True),
        sa.Column("last_queried_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"],
                                name="fk_payment_intents_invoice_id_invoices", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"],
                                name="fk_payment_intents_payment_id_payments", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["initiated_by"], ["users.id"],
                                name="fk_payment_intents_initiated_by_users", ondelete="SET NULL"),
        _tenant_fk("payment_intents"),
        sa.PrimaryKeyConstraint("id", name="pk_payment_intents"),
        sa.UniqueConstraint("tenant_id", "checkout_request_id",
                            name="uq_payment_intents_tenant_checkout_request_id"),
    )
    op.create_index("ix_payment_intents_tenant_id", "payment_intents", ["tenant_id"])
    op.create_index("ix_payment_intents_invoice_id", "payment_intents", ["invoice_id"])

    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"],
                                name="fk_payment_allocations_payment_id_payments",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"],
                                name="fk_payment_allocations_invoice_id_invoices",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["voided_by"], ["users.id"],
                                name="fk_payment_allocations_voided_by_users",
                                ondelete="SET NULL"),
        _tenant_fk("payment_allocations"),
        sa.PrimaryKeyConstraint("id", name="pk_payment_allocations"),
    )
    for col in ("tenant_id", "payment_id", "invoice_id"):
        op.create_index(f"ix_payment_allocations_{col}", "payment_allocations", [col])

    op.create_table(
        "invoice_adjustments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"],
                                name="fk_invoice_adjustments_invoice_id_invoices",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name="fk_invoice_adjustments_created_by_users",
                                ondelete="SET NULL"),
        _tenant_fk("invoice_adjustments"),
        sa.PrimaryKeyConstraint("id", name="pk_invoice_adjustments"),
    )
    for col in ("tenant_id", "invoice_id"):
        op.create_index(f"ix_invoice_adjustments_{col}", "invoice_adjustments", [col])

    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column("manual_mpesa_enabled", sa.Boolean(), nullable=False,
                                   server_default=sa.true()))
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("credit_term_days", sa.Integer(), nullable=False,
                                   server_default="30"))

    _opening_balance_invoices()

    with op.batch_alter_table("customers") as batch:
        batch.drop_column("credit_balance")


def _opening_balance_invoices():
    bind = op.get_bind()
    customers = sa.table("customers", sa.column("id", sa.Integer),
                         sa.column("tenant_id", sa.Integer),
                         sa.column("credit_balance", sa.Numeric(12, 2)))
    money = sa.Numeric(12, 2)
    invoices = sa.table(
        "invoices",
        sa.column("tenant_id", sa.Integer), sa.column("invoice_number", sa.String),
        sa.column("kind", sa.String), sa.column("customer_id", sa.Integer),
        sa.column("total", money), sa.column("amount_paid", money),
        sa.column("amount_adjusted", money), sa.column("status", sa.String),
        sa.column("issued_at", sa.DateTime), sa.column("due_date", sa.Date),
        sa.column("pay_token", sa.String), sa.column("etims_status", sa.String),
        sa.column("notes", sa.Text))
    rows = bind.execute(
        sa.select(customers.c.id, customers.c.tenant_id, customers.c.credit_balance)
        .where(customers.c.credit_balance > 0)
        .order_by(customers.c.tenant_id, customers.c.id)).all()
    counters = defaultdict(int)
    now, today = datetime.utcnow(), date.today()
    for customer_id, tenant_id, balance in rows:
        counters[tenant_id] += 1
        bind.execute(invoices.insert().values(
            tenant_id=tenant_id, invoice_number=f"INV{counters[tenant_id]:06d}",
            kind="opening_balance", customer_id=customer_id,
            total=Decimal(str(balance)), amount_paid=Decimal("0"),
            amount_adjusted=Decimal("0"), status="open", issued_at=now, due_date=today,
            pay_token=secrets.token_urlsafe(18), etims_status="not_submitted",
            notes="Opening balance carried over from the stored credit balance"))


def downgrade():
    bind = op.get_bind()
    paid_history = bind.execute(sa.text(
        "SELECT COUNT(*) FROM payment_allocations a JOIN invoices i ON i.id = a.invoice_id "
        "WHERE i.kind != 'opening_balance'")).scalar()
    if paid_history:
        raise RuntimeError(
            "Cannot downgrade below 0004: invoices other than opening balances have "
            "payments, and that history cannot be folded back into credit_balance.")

    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("credit_balance", sa.Numeric(precision=12, scale=2),
                                   nullable=False, server_default="0"))
    owed = bind.execute(sa.text(
        "SELECT customer_id, SUM(total - amount_adjusted - amount_paid) FROM invoices "
        "WHERE customer_id IS NOT NULL AND status IN ('open', 'partial') "
        "AND kind IN ('credit', 'opening_balance') GROUP BY customer_id")).all()
    customers = sa.table("customers", sa.column("id", sa.Integer),
                         sa.column("credit_balance", sa.Numeric(12, 2)))
    for customer_id, amount in owed:
        bind.execute(customers.update().where(customers.c.id == customer_id)
                     .values(credit_balance=amount))

    with op.batch_alter_table("customers") as batch:
        batch.drop_column("credit_term_days")
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("manual_mpesa_enabled")
    for table in ("invoice_adjustments", "payment_allocations", "payment_intents",
                  "payments", "invoices"):
        op.drop_table(table)
