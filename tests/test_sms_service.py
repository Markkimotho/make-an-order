"""Africa's Talking client must start lazily and never raise into callers."""
import importlib
import sys


def _fresh_sms(monkeypatch, **env):
    for key in ("AT_USERNAME", "AT_API_KEY", "AT_SENDER_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Config reads env at class definition, so reload it and the service.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    sys.modules.pop("config", None)
    sys.modules.pop("services.sms_service", None)
    return importlib.import_module("services.sms_service")


def test_import_without_credentials_does_not_raise(monkeypatch):
    _fresh_sms(monkeypatch)


def test_send_without_credentials_returns_false(monkeypatch):
    sms = _fresh_sms(monkeypatch)
    assert sms.SendSMS.send_payment_confirmation("0712345678", 100, "TXN1") is False


def test_client_initialises_once(monkeypatch):
    sms = _fresh_sms(monkeypatch, AT_USERNAME="sandbox", AT_API_KEY="k")
    calls = []
    monkeypatch.setattr(sms.africastalking, "initialize", lambda u, k: calls.append((u, k)))
    monkeypatch.setattr(sms.africastalking, "SMS", object(), raising=False)
    first = sms._client()
    second = sms._client()
    assert calls == [("sandbox", "k")]
    assert first is second is not None


def test_initialise_failure_returns_none(monkeypatch):
    sms = _fresh_sms(monkeypatch, AT_USERNAME="sandbox", AT_API_KEY="k")

    def boom(*a, **k):
        raise RuntimeError("Invalid username and/or api_key")

    monkeypatch.setattr(sms.africastalking, "initialize", boom)
    assert sms._client() is None
    assert sms.SendSMS.send_prescription_ready("0712345678", 7) is False
