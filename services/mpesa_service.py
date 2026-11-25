"""M-Pesa helpers that need no credentials.

Calls to Safaricom live in services/daraja (one client per business);
payment recording lives in services/billing. What stays here:

* :func:`format_phone` — Daraja's ``2547XXXXXXXX`` form
* :func:`parse_stk_callback` — flatten an STK push result
"""
from __future__ import annotations

from services.phone import normalize_ke_phone


def format_phone(phone: str) -> str:
    """Normalise to Daraja's ``2547XXXXXXXX`` / ``2541XXXXXXXX`` form.

    Same rules as :func:`services.phone.normalize_ke_phone` (raises
    ``InvalidPhone``, a ``ValueError``).
    """
    return normalize_ke_phone(phone)


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
