"""M-Pesa Daraja API client.

Encapsulates all HTTP calls to Safaricom's Daraja Lipa-Na-M-Pesa Online (STK
Push) endpoints. Uses ``requests`` (already in requirements.txt). All network
calls are wrapped in try/except — a failure raises :class:`MpesaError` so the
caller can return a sensible HTTP response without leaking SDK details.

Switches automatically between sandbox and production based on
``Config.MPESA_ENV`` (``"sandbox"`` or ``"production"``).

Reference: https://developer.safaricom.co.ke/Documentation
"""
from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)


SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE = "https://api.safaricom.co.ke"

# Module-level token cache: {"token": str, "expires_at": epoch_seconds}
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


class MpesaError(RuntimeError):
    """Raised when Daraja returns an error or is unreachable."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _base_url() -> str:
    return PRODUCTION_BASE if (Config.MPESA_ENV or "sandbox") == "production" else SANDBOX_BASE


def format_phone(phone: str) -> str:
    """Normalise a phone number to Daraja's ``2547XXXXXXXX`` / ``2541XXXXXXXX`` form.

    Accepts ``+2547...``, ``07...``, ``7...``, ``254...``, ``01...``.
    Raises :class:`ValueError` for clearly invalid input.
    """
    if not phone:
        raise ValueError("phone is required")
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("254"):
        normalised = digits
    elif digits.startswith("0") and len(digits) == 10:
        normalised = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in ("7", "1"):
        normalised = "254" + digits
    else:
        raise ValueError(f"unrecognised Kenyan phone format: {phone!r}")
    if len(normalised) != 12 or not normalised.startswith("254"):
        raise ValueError(f"unrecognised Kenyan phone format: {phone!r}")
    return normalised


def _timestamp() -> str:
    """Daraja timestamp format: yyyyMMddHHmmss."""
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _password(timestamp: str) -> str:
    """Base64(shortcode + passkey + timestamp)."""
    raw = f"{Config.MPESA_BUSINESS_SHORT_CODE}{Config.MPESA_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode("utf-8")).decode("utf-8")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def get_access_token(force_refresh: bool = False) -> str:
    """Fetch (and cache) an OAuth access token from Daraja.

    Tokens are valid for ~3600s. We cache for 3300s to be safe.
    """
    now = time.time()
    if (not force_refresh
            and _token_cache["token"]
            and _token_cache["expires_at"] > now):
        return _token_cache["token"]

    if not Config.MPESA_CONSUMER_KEY or not Config.MPESA_CONSUMER_SECRET:
        raise MpesaError("MPESA_CONSUMER_KEY / MPESA_CONSUMER_SECRET not configured")

    url = f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials"
    try:
        resp = requests.get(
            url,
            auth=(Config.MPESA_CONSUMER_KEY, Config.MPESA_CONSUMER_SECRET),
            timeout=15,
        )
    except requests.RequestException as e:
        raise MpesaError(f"Daraja OAuth network error: {e}") from e

    if resp.status_code != 200:
        raise MpesaError(f"Daraja OAuth failed: {resp.status_code} {resp.text}")

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise MpesaError(f"Daraja OAuth missing access_token: {data}")

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + 3300  # ~55 min
    return token


# --------------------------------------------------------------------------
# STK Push
# --------------------------------------------------------------------------

def stk_push(phone: str, amount, account_ref: str, description: str = "Payment") -> dict:
    """Initiate an STK Push (Lipa Na M-Pesa Online) request.

    ``amount`` is rounded to whole shillings (Daraja requires int).
    Returns Daraja's JSON response, which includes ``CheckoutRequestID``.
    """
    msisdn = format_phone(phone)
    amount_int = int(round(float(amount)))
    if amount_int < 1:
        raise ValueError("amount must be >= 1 KES")

    timestamp = _timestamp()
    payload = {
        "BusinessShortCode": Config.MPESA_BUSINESS_SHORT_CODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount_int,
        "PartyA": msisdn,
        "PartyB": Config.MPESA_BUSINESS_SHORT_CODE,
        "PhoneNumber": msisdn,
        "CallBackURL": Config.MPESA_CALLBACK_URL,
        "AccountReference": (account_ref or "")[:12],   # Daraja max 12 chars
        "TransactionDesc": (description or "Payment")[:13],
    }

    token = get_access_token()
    url = f"{_base_url()}/mpesa/stkpush/v1/processrequest"

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise MpesaError(f"Daraja STK Push network error: {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise MpesaError(f"Daraja STK Push non-JSON response: {resp.status_code} {resp.text}")

    if resp.status_code != 200 or str(data.get("ResponseCode", "")) != "0":
        logger.warning("Daraja STK Push failed: %s", data)
        raise MpesaError(data.get("errorMessage") or data.get("ResponseDescription") or str(data))

    logger.info("STK Push initiated: %s", data.get("CheckoutRequestID"))
    return data


def query_stk_status(checkout_request_id: str) -> dict:
    """Query the result of a previously-initiated STK Push.

    Useful when the callback hasn't arrived yet (network blip, sandbox quirks).
    """
    if not checkout_request_id:
        raise ValueError("checkout_request_id is required")

    timestamp = _timestamp()
    payload = {
        "BusinessShortCode": Config.MPESA_BUSINESS_SHORT_CODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    token = get_access_token()
    url = f"{_base_url()}/mpesa/stkpushquery/v1/query"

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise MpesaError(f"Daraja Query network error: {e}") from e

    try:
        return resp.json()
    except ValueError:
        raise MpesaError(f"Daraja Query non-JSON response: {resp.status_code} {resp.text}")


# --------------------------------------------------------------------------
# Callback parsing
# --------------------------------------------------------------------------

def parse_stk_callback(body: dict) -> dict:
    """Parse Daraja's STK Push callback payload into a flat dict.

    Daraja posts ``Body.stkCallback`` with ``ResultCode``, ``ResultDesc``,
    ``CheckoutRequestID`` and (on success) a ``CallbackMetadata.Item`` list.

    Returns::

        {
            "checkout_request_id": str,
            "result_code": int,
            "result_desc": str,
            "success": bool,
            "amount": float | None,
            "mpesa_receipt": str | None,
            "phone_number": str | None,
            "transaction_date": str | None,
        }
    """
    callback = (body or {}).get("Body", {}).get("stkCallback", {}) or {}
    out = {
        "checkout_request_id": callback.get("CheckoutRequestID"),
        "result_code": callback.get("ResultCode"),
        "result_desc": callback.get("ResultDesc"),
        "success": callback.get("ResultCode") == 0,
        "amount": None,
        "mpesa_receipt": None,
        "phone_number": None,
        "transaction_date": None,
    }
    items = (callback.get("CallbackMetadata") or {}).get("Item") or []
    for item in items:
        name = item.get("Name")
        value = item.get("Value")
        if name == "Amount":
            out["amount"] = float(value) if value is not None else None
        elif name == "MpesaReceiptNumber":
            out["mpesa_receipt"] = str(value) if value is not None else None
        elif name == "PhoneNumber":
            out["phone_number"] = str(value) if value is not None else None
        elif name == "TransactionDate":
            out["transaction_date"] = str(value) if value is not None else None
    return out
