"""Matching a Paybill account reference to who paid.

Order: an invoice number, then any registered matcher. Later steps add
matchers (a school admission number, a duka's account code), each
returning a Match or None.
"""
import re
from dataclasses import dataclass

from models import Invoice


@dataclass
class Match:
    invoice: object = None
    customer_id: int | None = None


C2B_MATCHERS = []


def normalise_reference(reference):
    """Upper case, no spaces or punctuation: 'inv 000123' -> 'INV000123'."""
    return re.sub(r"[^A-Z0-9]", "", (reference or "").upper())


def match_reference(reference):
    ref = normalise_reference(reference)
    if not ref:
        return Match()
    invoice = Invoice.query.filter_by(invoice_number=ref).first()
    if invoice is not None:
        return Match(invoice=invoice, customer_id=invoice.customer_id)
    for matcher in C2B_MATCHERS:
        found = matcher(ref)
        if found is not None:
            return found
    return Match()
