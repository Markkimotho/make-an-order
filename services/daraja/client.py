"""Safaricom Daraja, with one business's own credentials.

Money from an STK push or a Paybill payment settles straight into that
business's Till or Paybill. Paybill pushes use CustomerPayBillOnline with
the shortcode as PartyB; Till pushes use CustomerBuyGoodsOnline with the
store number as BusinessShortCode and the till number as PartyB.
"""
import base64
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import requests

from config import Config
from services.daraja import tokens
from services.phone import normalize_ke_phone

logger = logging.getLogger(__name__)

BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke",
             "production": "https://api.safaricom.co.ke"}
BUSY = "500.001.1001"    # a prompt is already on the phone / still being processed


class DarajaError(RuntimeError):
    """Daraja refused or failed a request."""


class DarajaNotConfigured(DarajaError):
    """The business has not entered its Daraja credentials."""


class DarajaBusy(DarajaError):
    """The customer already has an M-Pesa prompt open; retry in a minute."""


class DarajaUnavailable(DarajaError):
    """Daraja could not be reached (network, timeout, 5xx)."""


@dataclass
class QueryResult:
    state: str            # paid | failed | processing
    result_code: str | None
    result_desc: str | None


def callback_url(tenant, kind):
    """Public URL Safaricom calls back. Never contains 'mpesa' or 'safaricom':
    Daraja's URL registration refuses those words."""
    base = (Config.APP_URL or "").rstrip("/")
    return f"{base}/api/payments/daraja/{kind}/{tenant.slug}/{tenant.callback_token}"


def stk_amount(balance):
    """STK takes whole shillings: round up, never under-collect."""
    return max(int(math.ceil(Decimal(str(balance)))), 1)


class DarajaClient:
    def __init__(self, tenant, timeout=30):
        self.tenant = tenant
        self.timeout = timeout
        missing = [name for name in ("daraja_shortcode", "daraja_consumer_key",
                                     "daraja_consumer_secret")
                   if not getattr(tenant, name)]
        if missing:
            raise DarajaNotConfigured(f"Daraja is not set up for this business ({', '.join(missing)})")
        if tenant.daraja_shortcode_type == "till" and not tenant.daraja_till_number:
            raise DarajaNotConfigured("Till number is missing")
        self.base = BASE_URLS.get(tenant.daraja_env or "sandbox", BASE_URLS["sandbox"])

    # --- auth ---------------------------------------------------------------

    def _fetch_token(self):
        try:
            resp = requests.get(f"{self.base}/oauth/v1/generate?grant_type=client_credentials",
                                auth=(self.tenant.daraja_consumer_key,
                                      self.tenant.daraja_consumer_secret),
                                timeout=15)
        except requests.RequestException as e:
            raise DarajaUnavailable(f"Daraja OAuth network error: {e}") from e
        if resp.status_code != 200:
            raise DarajaError(f"Daraja OAuth failed: {resp.status_code}")
        data = resp.json()
        if not data.get("access_token"):
            raise DarajaError("Daraja OAuth returned no access_token")
        return data["access_token"], data.get("expires_in", "3599")

    def _headers(self):
        return {"Authorization": f"Bearer {tokens.get_token(self.tenant.id, self._fetch_token)}"}

    def _post(self, path, payload, timeout=None):
        try:
            resp = requests.post(f"{self.base}{path}", json=payload, headers=self._headers(),
                                 timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise DarajaUnavailable(f"Daraja network error: {e}") from e
        # Daraja answers "busy" (500.001.1001) with HTTP 500 and a JSON body,
        # so 500 is parsed; gateway errors mean Daraja is unreachable.
        if resp.status_code in (502, 503, 504):
            raise DarajaUnavailable(f"Daraja unavailable: {resp.status_code}")
        try:
            return resp.status_code, resp.json()
        except ValueError:
            raise DarajaUnavailable(f"Daraja returned non-JSON ({resp.status_code})")

    # --- STK push -------------------------------------------------------------

    def _password(self, timestamp):
        if not self.tenant.daraja_passkey:
            raise DarajaNotConfigured("Daraja passkey is missing")
        raw = f"{self.tenant.daraja_shortcode}{self.tenant.daraja_passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    def _stk_base(self):
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return {"BusinessShortCode": self.tenant.daraja_shortcode,
                "Password": self._password(timestamp), "Timestamp": timestamp}

    def stk_push(self, phone, amount, account_ref, description="Payment"):
        msisdn = normalize_ke_phone(phone)
        till = self.tenant.daraja_shortcode_type == "till"
        payload = {
            **self._stk_base(),
            "TransactionType": "CustomerBuyGoodsOnline" if till else "CustomerPayBillOnline",
            "Amount": stk_amount(amount),
            "PartyA": msisdn,
            "PartyB": self.tenant.daraja_till_number if till else self.tenant.daraja_shortcode,
            "PhoneNumber": msisdn,
            "CallBackURL": callback_url(self.tenant, "stk"),
            "AccountReference": (account_ref or "")[:12],
            "TransactionDesc": (description or "Payment")[:13],
        }
        status, data = self._post("/mpesa/stkpush/v1/processrequest", payload)
        if data.get("errorCode") == BUSY:
            raise DarajaBusy(data.get("errorMessage") or "A payment prompt is already open")
        if status != 200 or str(data.get("ResponseCode", "")) != "0":
            raise DarajaError(data.get("errorMessage") or data.get("ResponseDescription")
                              or f"STK push failed ({status})")
        return data

    def query(self, checkout_request_id, timeout=5):
        """State of an STK push. "processing" means ask again later."""
        payload = {**self._stk_base(), "CheckoutRequestID": checkout_request_id}
        status, data = self._post("/mpesa/stkpushquery/v1/query", payload, timeout=timeout)
        if data.get("errorCode") == BUSY:
            return QueryResult("processing", BUSY, data.get("errorMessage"))
        code = data.get("ResultCode")
        if code is None:
            raise DarajaUnavailable(data.get("errorMessage") or f"STK query failed ({status})")
        code = str(code)
        return QueryResult("paid" if code == "0" else "failed", code, data.get("ResultDesc"))

    # --- C2B ----------------------------------------------------------------

    def register_c2b(self):
        """Tell Safaricom where to send Paybill confirmations. Always
        ResponseType Completed: if our validation URL is slow or down, the
        customer's payment still goes through instead of being cancelled."""
        payload = {"ShortCode": self.tenant.daraja_shortcode,
                   "ResponseType": "Completed",
                   "ConfirmationURL": callback_url(self.tenant, "c2b"),
                   "ValidationURL": callback_url(self.tenant, "c2b") + "/validate"}
        status, data = self._post("/mpesa/c2b/v1/registerurl", payload)
        if status != 200 or str(data.get("ResponseCode", "0")) not in ("0", "00000000"):
            raise DarajaError(data.get("errorMessage") or data.get("ResponseDescription")
                              or f"C2B registration failed ({status})")
        return data
