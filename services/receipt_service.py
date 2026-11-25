"""Receipt builders for completed POS transactions.

Three output formats are supported:

* :func:`build_receipt`           — structured ``dict`` (used for JSON, HTML, PDF)
* :func:`format_sms_receipt`      — short single-paragraph string for SMS
* :func:`format_thermal_receipt`  — 42-column plain text for an 80mm ESC/POS printer

All formatters look up the transaction fresh, so they reflect the latest
state (refunds, voids).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from config import Config
from models import (Batch, Customer, Invoice, LineBatch, Payment, PaymentAllocation,
                    Transaction, User, db)


# --------------------------------------------------------------------------
# Structured receipt
# --------------------------------------------------------------------------

def _business():
    """The bound business's name and licence details for the receipt header."""
    from auth.tenancy import current_tenant_id
    from models import Tenant
    tenant_id = current_tenant_id()
    tenant = db.session.get(Tenant, tenant_id) if tenant_id is not None else None
    if tenant is None:
        return {"name": Config.BUSINESS_NAME, "kra_pin": None, "ppb_premises_license": None}
    return {"name": tenant.name, "kra_pin": tenant.kra_pin,
            "ppb_premises_license": tenant.ppb_premises_license}


def _batches(line):
    """Batch numbers and expiry dates a sale line came out of."""
    rows = (db.session.query(Batch.batch_number, Batch.expiry_date, LineBatch.quantity)
            .join(LineBatch, LineBatch.batch_id == Batch.id)
            .filter(LineBatch.transaction_item_id == line.id)
            .order_by(Batch.expiry_date, Batch.id).all())
    return [{"batch_number": number, "expiry_date": expiry.isoformat() if expiry else None,
             "quantity": quantity} for number, expiry, quantity in rows]


def _payments(txn):
    invoice = (Invoice.query.filter_by(transaction_id=txn.id)
               .order_by(Invoice.id.desc()).first())
    if invoice is None:
        return None, []
    ids = [a.payment_id for a in PaymentAllocation.query.filter(
        PaymentAllocation.invoice_id == invoice.id, PaymentAllocation.voided_at.is_(None))]
    payments = (Payment.query.filter(Payment.id.in_(ids)).order_by(Payment.id).all()
                if ids else [])
    return invoice, [{"method": p.method, "amount": float(p.amount),
                      "reference": p.mpesa_receipt or p.bank_reference,
                      "bank_name": p.bank_name, "verified": bool(p.verified)}
                     for p in payments]


def build_receipt(transaction_id: int) -> dict[str, Any]:
    """Return a fully-resolved receipt dict for the given transaction."""
    txn = db.session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError(f"transaction {transaction_id} not found")

    customer = db.session.get(Customer, txn.customer_id) if txn.customer_id else None
    cashier  = db.session.get(User, txn.cashier_id) if txn.cashier_id else None
    served   = db.session.get(User, txn.served_by_id) if txn.served_by_id else None
    invoice, payments = _payments(txn)

    return {
        "business": _business(),
        "transaction": {
            "id":              txn.id,
            "ref":             txn.transaction_ref,
            "status":          txn.status,
            "created_at":      txn.created_at.isoformat() if txn.created_at else None,
            "payment_method":  txn.payment_method,
            "mpesa_ref":       txn.mpesa_ref,
            "card_ref":        txn.card_ref,
            "notes":           txn.notes,
            "invoice_number":  invoice.invoice_number if invoice else None,
        },
        "cashier": {
            "id":   getattr(cashier, "id", None),
            "name": getattr(cashier, "name", None) or getattr(cashier, "email", None),
        },
        "served_by": {
            "id":   getattr(served, "id", None),
            "name": getattr(served, "name", None) or getattr(served, "email", None),
        },
        "customer": (
            {
                "id":           customer.id,
                "name":         customer.name,
                "phone_number": customer.phone_number,
                "code":         customer.code,
            }
            if customer else None
        ),
        "items": [
            {
                "name":       i.item_name,
                "quantity":   float(i.quantity),
                "unit_price": float(i.unit_price),
                "discount":   float(i.discount or 0),
                "line_total": float(i.line_total),
                "batches":    _batches(i),
            }
            for i in txn.items
        ],
        "payments": payments,
        "unverified": any(not p["verified"] for p in payments),
        "totals": {
            "subtotal":        float(txn.subtotal or 0),
            "discount_amount": float(txn.discount_amount or 0),
            "tax_amount":      float(txn.tax_amount or 0),
            "total":           float(txn.total or 0),
        },
    }


# --------------------------------------------------------------------------
# SMS receipt
# --------------------------------------------------------------------------

def format_sms_receipt(transaction_id: int) -> str:
    """Short SMS-safe receipt string (< 160 chars typical)."""
    r = build_receipt(transaction_id)
    biz = r["business"]["name"]
    total = r["totals"]["total"]
    ref = r["transaction"]["ref"]
    method = (r["transaction"]["payment_method"] or "").upper()
    extras = []
    if r["transaction"]["mpesa_ref"]:
        extras.append(f"MPESA:{r['transaction']['mpesa_ref']}")
    if r["transaction"]["card_ref"]:
        extras.append(f"CARD:{r['transaction']['card_ref']}")
    extras_str = (" " + " ".join(extras)) if extras else ""
    return (
        f"{biz}: Payment of KES {total:.2f} received. "
        f"Ref:{ref} ({method}).{extras_str} Thank you!"
    )


# --------------------------------------------------------------------------
# Thermal receipt
# --------------------------------------------------------------------------

WIDTH = 42  # 80mm @ Font A on most ESC/POS printers


def _line(char: str = "-") -> str:
    return char * WIDTH


def _center(text: str) -> str:
    text = text[:WIDTH]
    pad = (WIDTH - len(text)) // 2
    return (" " * pad) + text


def _two_col(left: str, right: str) -> str:
    """Left-justify ``left``, right-justify ``right`` within :data:`WIDTH`."""
    right = str(right)
    left = str(left)
    space = WIDTH - len(right) - 1
    if space < 1:
        # Right value too wide — wrap left underneath.
        return f"{left}\n{right.rjust(WIDTH)}"
    if len(left) > space:
        left = left[: space - 1] + "…"
    return f"{left.ljust(space)} {right}"


def format_thermal_receipt(transaction_id: int) -> str:
    """Render the receipt as plain text suitable for piping to an ESC/POS printer.

    Uses a fixed 42-column layout. Caller is responsible for the cut/feed
    control codes — this just returns the visible body.
    """
    r = build_receipt(transaction_id)
    out: list[str] = []

    # Header
    out.append(_center(r["business"]["name"]))
    out.append(_center("SALES RECEIPT"))
    out.append(_line())
    created = r["transaction"]["created_at"] or datetime.utcnow().isoformat()
    out.append(_two_col("Date:", created.replace("T", " ")[:19]))
    out.append(_two_col("Ref:",  r["transaction"]["ref"]))
    out.append(_two_col("Cashier:", r["cashier"]["name"] or "—"))
    if r["served_by"]["name"] and r["served_by"]["id"] != r["cashier"]["id"]:
        out.append(_two_col("Served by:", r["served_by"]["name"]))
    if r["customer"]:
        out.append(_two_col("Customer:", r["customer"]["name"]))
    out.append(_line())

    # Items
    for it in r["items"]:
        qty = f'{it["quantity"]:g} x {it["unit_price"]:.2f}'
        out.append(it["name"][:WIDTH])
        out.append(_two_col(f"  {qty}", f'{it["line_total"]:.2f}'))
        if it["discount"]:
            out.append(_two_col("  discount", f'-{it["discount"]:.2f}'))
        for b in it.get("batches") or []:
            out.append(f'  Batch {b["batch_number"]} exp {b["expiry_date"] or "-"}'[:WIDTH])

    out.append(_line())
    out.append(_two_col("Subtotal", f'{r["totals"]["subtotal"]:.2f}'))
    if r["totals"]["discount_amount"]:
        out.append(_two_col("Discount", f'-{r["totals"]["discount_amount"]:.2f}'))
    if r["totals"]["tax_amount"]:
        out.append(_two_col(f'VAT ({Config.TAX_RATE*100:.0f}%)',
                            f'{r["totals"]["tax_amount"]:.2f}'))
    out.append(_two_col("TOTAL", f'{r["totals"]["total"]:.2f}'))
    out.append(_line())
    out.append(_two_col("Paid via", (r["transaction"]["payment_method"] or "").upper()))
    if r["transaction"]["mpesa_ref"]:
        out.append(_two_col("M-Pesa Ref", r["transaction"]["mpesa_ref"]))
    if r["transaction"]["card_ref"]:
        out.append(_two_col("Card Ref", r["transaction"]["card_ref"]))
    for p in r.get("payments") or []:
        if p["method"] == "bank":
            out.append(_two_col(f'Bank {p["bank_name"] or ""}'.strip(), p["reference"] or ""))
    if r.get("unverified"):
        out.append(_center("Payment awaiting bank confirmation"))
    out.append("")
    out.append(_center("Thank you for your business!"))
    out.append("")
    return "\n".join(out)
