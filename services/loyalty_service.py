"""Loyalty-points service.

Points are integer-valued. Default rule: ``LOYALTY_POINTS_RATIO`` KES of net
spend earns 1 point. 1 point = 1 KES discount when redeemed.

All mutations are logged to :class:`LoyaltyTransaction` so customer-facing
``/customers/<id>/loyalty`` returns a full audit trail.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from config import Config
from models import Customer, LoyaltyTransaction, db


class LoyaltyError(RuntimeError):
    """Raised on insufficient balance, invalid input, or unknown customer."""


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

def get_balance(customer_id: int) -> int:
    """Return the current loyalty point balance for ``customer_id``."""
    customer = Customer.query.get(customer_id)
    if customer is None:
        raise LoyaltyError(f"customer {customer_id} not found")
    return int(customer.loyalty_points or 0)


# --------------------------------------------------------------------------
# Award
# --------------------------------------------------------------------------

def award_points(customer_id: int, transaction_id: Optional[int], total_amount) -> LoyaltyTransaction:
    """Award points to ``customer_id`` based on ``total_amount``.

    Formula: ``floor(total_amount / LOYALTY_POINTS_RATIO)``.
    Returns the persisted :class:`LoyaltyTransaction` row.
    Caller controls the surrounding transaction; this function flushes but
    does NOT commit so it can compose with the POS checkout flow.
    """
    customer = Customer.query.get(customer_id)
    if customer is None:
        raise LoyaltyError(f"customer {customer_id} not found")

    ratio = max(int(Config.LOYALTY_POINTS_RATIO or 1), 1)
    points = int(Decimal(str(total_amount)) // ratio)
    if points <= 0:
        # Still log a zero-point movement? No — keeps the ledger tidy.
        return LoyaltyTransaction(
            customer_id=customer_id,
            transaction_id=transaction_id,
            points_earned=0,
            points_redeemed=0,
            balance_after=int(customer.loyalty_points or 0),
            note="no points awarded (below threshold)",
        )

    customer.loyalty_points = int(customer.loyalty_points or 0) + points
    log = LoyaltyTransaction(
        customer_id=customer_id,
        transaction_id=transaction_id,
        points_earned=points,
        points_redeemed=0,
        balance_after=customer.loyalty_points,
        note=f"earned on KES {total_amount}",
    )
    db.session.add(log)
    db.session.flush()
    return log


# --------------------------------------------------------------------------
# Redeem
# --------------------------------------------------------------------------

def redeem_points(customer_id: int, points: int, transaction_id: Optional[int]) -> LoyaltyTransaction:
    """Redeem ``points`` from ``customer_id``.

    Raises :class:`LoyaltyError` if the customer lacks the balance.
    Caller is responsible for translating points → KES discount and applying
    it to the transaction total.
    """
    points = int(points or 0)
    if points <= 0:
        raise LoyaltyError("points must be > 0")

    customer = Customer.query.get(customer_id)
    if customer is None:
        raise LoyaltyError(f"customer {customer_id} not found")

    current = int(customer.loyalty_points or 0)
    if points > current:
        raise LoyaltyError(f"insufficient points: have {current}, requested {points}")

    customer.loyalty_points = current - points
    log = LoyaltyTransaction(
        customer_id=customer_id,
        transaction_id=transaction_id,
        points_earned=0,
        points_redeemed=points,
        balance_after=customer.loyalty_points,
        note=f"redeemed {points} pts",
    )
    db.session.add(log)
    db.session.flush()
    return log
