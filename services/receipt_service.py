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
from models import Customer, Transaction, User, db


# --------------------------------------------------------------------------
# Structured receipt
# --------------------------------------------------------------------------

def build_receipt(transaction_id: int) -> dict[str, Any]:
    """Return a fully-resolved receipt dict for the given transaction."""
    txn = db.session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError(f"transaction {transaction_id} not found")

    customer = db.session.get(Customer, txn.customer_id) if txn.customer_id else None
    cashier  = db.session.get(User, txn.cashier_id) if txn.cashier_id else None

    return {
        "business": {
            "name": Config.BUSINESS_NAME,
        },
        "transaction": {
            "id":              txn.id,
            "ref":             txn.transaction_ref,
            "status":          txn.status,
            "created_at":      txn.created_at.isoformat() if txn.created_at else None,
            "payment_method":  txn.payment_method,
            "mpesa_ref":       txn.mpesa_ref,
            "card_ref":        txn.card_ref,
            "notes":           txn.notes,
        },
        "cashier": {
            "id":   getattr(cashier, "id", None),
            "name": getattr(cashier, "name", None) or getattr(cashier, "email", None),
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
            }
            for i in txn.items
        ],
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
    out.append("")
    out.append(_center("Thank you for your business!"))
    out.append("")
    return "\n".join(out)
