"""Custom column types."""
import os

from cryptography.fernet import Fernet
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


class SecretsKeyMissing(RuntimeError):
    """A secret was about to be stored but TENANT_SECRETS_KEY is not set."""


def _secrets_key():
    key = None
    try:
        from flask import current_app
        key = current_app.config.get("TENANT_SECRETS_KEY")
    except RuntimeError:
        pass  # no app context: scripts, migrations, unit tests
    return key or os.environ.get("TENANT_SECRETS_KEY")


def secrets_key_configured():
    return bool(_secrets_key())


class EncryptedString(TypeDecorator):
    """A string column stored Fernet-encrypted, returned as plaintext.

    For per-tenant secrets (Daraja keys and passkey, eTIMS token). The key
    comes from ``TENANT_SECRETS_KEY`` (app config, else environment); it is
    never stored in the database. Writing a non-null value with no key
    raises instead of storing plaintext. ``length`` is the ciphertext column
    size: Fernet output is about 1.4x the input plus ~60 bytes.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        key = _secrets_key()
        if not key:
            raise SecretsKeyMissing("TENANT_SECRETS_KEY is not set; refusing to store a secret")
        return Fernet(key.encode()).encrypt(str(value).encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        key = _secrets_key()
        if not key:
            raise SecretsKeyMissing("TENANT_SECRETS_KEY is not set; cannot read a secret")
        return Fernet(key.encode()).decrypt(value.encode()).decode()
