"""Africa's Talking client must start lazily and never raise into callers."""
import importlib
import sys


def _fresh_sms(monkeypatch, **env):
    """Import a fresh copy of the service with exactly this environment.

    monkeypatch restores the original modules afterwards, so the rest of the
    suite keeps the copy that the API blueprints (and the conftest SMS fake)
    are bound to.
    """
    import services
    for key in ("AT_USERNAME", "AT_API_KEY", "AT_SENDER_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Config reads env at class definition, so re-import it and the service.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    monkeypatch.delitem(sys.modules, "services.sms_service", raising=False)
    monkeypatch.setattr(services, "sms_service", None, raising=False)
    return importlib.import_module("services.sms_service")


def test_import_without_credentials_does_not_raise(monkeypatch):
    _fresh_sms(monkeypatch)


def test_send_without_credentials_returns_false(app, monkeypatch):
    sms = _fresh_sms(monkeypatch)
    assert sms.SendSMS.send_payment_confirmation("0712345678", 100, "TXN1") is False
    from models import SmsMessage
    row = SmsMessage.query.one()
    assert (row.status, row.error) == ("failed", "SMS not configured")


def test_client_initialises_once(monkeypatch):
    sms = _fresh_sms(monkeypatch, AT_USERNAME="sandbox", AT_API_KEY="k")
    calls = []
    monkeypatch.setattr(sms.africastalking, "initialize", lambda u, k: calls.append((u, k)))
    monkeypatch.setattr(sms.africastalking, "SMS", object(), raising=False)
    first = sms._client()
    second = sms._client()
    assert calls == [("sandbox", "k")]
    assert first is second is not None


def test_initialise_failure_returns_none(app, monkeypatch):
    sms = _fresh_sms(monkeypatch, AT_USERNAME="sandbox", AT_API_KEY="k")

    def boom(*a, **k):
        raise RuntimeError("Invalid username and/or api_key")

    monkeypatch.setattr(sms.africastalking, "initialize", boom)
    assert sms._client() is None
    assert sms.SendSMS.send_prescription_ready("0712345678", 7) is False
