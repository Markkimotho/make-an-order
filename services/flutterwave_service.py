"""Flutterwave card-payments client.

Wraps the two endpoints we need from Flutterwave's v3 REST API:

* ``POST /v3/payments``               — create a hosted payment link
* ``GET  /v3/transactions/<id>/verify`` — verify a transaction by ID
* webhook signature verification using ``FLW_WEBHOOK_SECRET``

Reference: https://developer.flutterwave.com/docs
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.flutterwave.com"


class FlutterwaveError(RuntimeError):
    """Raised when Flutterwave returns an error or is unreachable."""


def _auth_headers() -> dict[str, str]:
    if not Config.FLW_SECRET_KEY:
        raise FlutterwaveError("FLW_SECRET_KEY not configured")
    return {
        "Authorization": f"Bearer {Config.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------
# Hosted payment link
# --------------------------------------------------------------------------

def create_payment_link(
    tx_ref: str,
    amount,
    currency: str,
    customer_email: str,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    redirect_url: str | None = None,
    description: str | None = None,
) -> dict:
    """Create a Flutterwave hosted-checkout link.

    ``tx_ref`` MUST be unique per attempt (we use the POS transaction_ref).
    Returns ``{"payment_link": str, "raw": <api response>}``.
    """
    if not tx_ref:
        raise ValueError("tx_ref is required")
    if not customer_email:
        raise ValueError("customer_email is required")

    payload: dict[str, Any] = {
        "tx_ref": tx_ref,
        "amount": str(amount),
        "currency": currency or "KES",
        "redirect_url": redirect_url or "",
        "customer": {
            "email": customer_email,
            "name": customer_name or "",
            "phonenumber": customer_phone or "",
        },
        "customizations": {
            "title": Config.BUSINESS_NAME or "POS Payment",
            "description": description or f"Payment {tx_ref}",
        },
    }

    try:
        resp = requests.post(
            f"{BASE_URL}/v3/payments",
            json=payload,
            headers=_auth_headers(),
            timeout=30,
        )
    except requests.RequestException as e:
        raise FlutterwaveError(f"Flutterwave network error: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise FlutterwaveError(f"Flutterwave non-JSON response: {resp.status_code} {resp.text}")

    if resp.status_code != 200 or data.get("status") != "success":
        logger.warning("Flutterwave create_payment_link failed: %s", data)
        raise FlutterwaveError(data.get("message") or "create_payment_link failed")

    link = (data.get("data") or {}).get("link")
    if not link:
        raise FlutterwaveError(f"Flutterwave response missing link: {data}")
    return {"payment_link": link, "raw": data}


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------

def verify_transaction(transaction_id: str | int) -> dict:
    """Verify a Flutterwave transaction by its numeric ID.

    Returns a flattened dict with ``status``, ``amount``, ``currency``,
    ``tx_ref``, ``flw_ref``, ``raw``.
    """
    if not transaction_id:
        raise ValueError("transaction_id is required")

    url = f"{BASE_URL}/v3/transactions/{transaction_id}/verify"
    try:
        resp = requests.get(url, headers=_auth_headers(), timeout=30)
    except requests.RequestException as e:
        raise FlutterwaveError(f"Flutterwave network error: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise FlutterwaveError(f"Flutterwave non-JSON response: {resp.status_code} {resp.text}")

    if resp.status_code != 200 or data.get("status") != "success":
        logger.warning("Flutterwave verify failed: %s", data)
        raise FlutterwaveError(data.get("message") or "verify failed")

    body = data.get("data") or {}
    return {
        "status":   body.get("status"),         # 'successful' | 'failed' | 'pending'
        "amount":   body.get("amount"),
        "currency": body.get("currency"),
        "tx_ref":   body.get("tx_ref"),
        "flw_ref":  body.get("flw_ref"),
        "raw":      data,
    }


# --------------------------------------------------------------------------
# Webhook signature verification
# --------------------------------------------------------------------------

def validate_webhook_signature(payload: bytes | str, signature: str | None) -> bool:
    """Verify the ``verif-hash`` header sent by Flutterwave webhooks.

    Flutterwave's documented mechanism is a shared-secret comparison: the
    dashboard-configured ``secret hash`` is sent verbatim as the ``verif-hash``
    header. We additionally support an HMAC-SHA256 of the body for callers who
    prefer that scheme (some integration guides recommend it).

    Constant-time comparison via :func:`hmac.compare_digest`.
    """
    secret = Config.FLW_WEBHOOK_SECRET
    if not secret:
        logger.error("FLW_WEBHOOK_SECRET not configured — refusing webhook")
        return False
    if not signature:
        return False

    # 1. Plain shared-secret match (Flutterwave's default).
    if hmac.compare_digest(signature, secret):
        return True

    # 2. HMAC-SHA256(body, secret) hex digest fallback.
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
