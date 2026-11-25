"""A stand-in for Safaricom's Daraja HTTP API, shared by the tests."""
from cryptography.fernet import Fernet

from auth.tenancy import unscoped
from models import Tenant, db
from services.daraja import tokens


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeDaraja:
    """Records calls. Answers from a queue per URL suffix, or a default."""

    def __init__(self):
        self.calls = []
        self.token_fetches = 0
        self.answers = {}
        self.defaults = {}
        self._push_n = 0

    def answer(self, path_end, status, body):
        self.answers.setdefault(path_end, []).append((status, body))

    def default(self, path_end, status, body):
        self.defaults[path_end] = (status, body)

    def _reply(self, url, payload):
        for path_end, queue in self.answers.items():
            if url.endswith(path_end) and queue:
                status, body = queue.pop(0)
                if isinstance(body, Exception):
                    raise body
                return Resp(status, body)
        for path_end, (status, body) in self.defaults.items():
            if url.endswith(path_end):
                if isinstance(body, Exception):
                    raise body
                return Resp(status, body)
        if url.endswith("/processrequest"):
            self._push_n += 1
            return Resp(200, {"ResponseCode": "0",
                              "CheckoutRequestID": f"ws_CO_{self._push_n}",
                              "MerchantRequestID": f"m_{self._push_n}"})
        raise AssertionError(f"unexpected Daraja call {url}")

    def get(self, url, auth=None, timeout=None):
        self.token_fetches += 1
        self.calls.append(("GET", url, auth, None))
        return Resp(200, {"access_token": f"tok{self.token_fetches}", "expires_in": "3599"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, headers, json))
        return self._reply(url, json)

    def posts(self, path_end):
        return [c[3] for c in self.calls if c[0] == "POST" and c[1].endswith(path_end)]


def install(monkeypatch):
    fake = FakeDaraja()
    import services.daraja.client as client_module
    monkeypatch.setattr(client_module.requests, "get", fake.get)
    monkeypatch.setattr(client_module.requests, "post", fake.post)
    monkeypatch.setattr(tokens, "_get_redis", lambda: None)
    tokens._memory.clear()
    return fake


def configure(app, monkeypatch, tenant_id, kind="paybill"):
    """Give an existing tenant Daraja credentials. Returns (slug, token)."""
    if not app.config.get("TENANT_SECRETS_KEY"):
        monkeypatch.setitem(app.config, "TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    with unscoped():
        t = db.session.get(Tenant, tenant_id)
        t.daraja_shortcode = "600123"
        t.daraja_shortcode_type = kind
        t.daraja_till_number = "5551234" if kind == "till" else None
        t.daraja_consumer_key, t.daraja_consumer_secret = "ck", "cs"
        t.daraja_passkey = "pk"
        db.session.commit()
        return t.slug, t.callback_token


def stk_callback(checkout_id, receipt="SJK0000001", amount=100, code=0, phone="254712345678"):
    body = {"Body": {"stkCallback": {
        "MerchantRequestID": "m", "CheckoutRequestID": checkout_id, "ResultCode": code,
        "ResultDesc": "The service request is processed successfully." if code == 0
        else "Request cancelled by user"}}}
    if code == 0:
        body["Body"]["stkCallback"]["CallbackMetadata"] = {"Item": [
            {"Name": "Amount", "Value": amount},
            {"Name": "MpesaReceiptNumber", "Value": receipt},
            {"Name": "TransactionDate", "Value": 20261124143000},
            {"Name": "PhoneNumber", "Value": int(phone)},
        ]}
    return body


def c2b(trans_id, amount, bill_ref, balance=None, msisdn="2547****678"):
    body = {"TransactionType": "Pay Bill", "TransID": trans_id, "TransTime": "20261124143000",
            "TransAmount": str(amount), "BusinessShortCode": "600123", "BillRefNumber": bill_ref,
            "MSISDN": msisdn, "FirstName": "WANJIKU", "MiddleName": "", "LastName": "KAMAU"}
    if balance is not None:
        body["OrgAccountBalance"] = str(balance)
    return body
