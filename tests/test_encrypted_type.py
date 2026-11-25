import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select

from models.types import EncryptedString, SecretsKeyMissing

KEY = Fernet.generate_key().decode()


def _table():
    md = MetaData()
    t = Table("secrets", md,
              Column("id", Integer, primary_key=True),
              Column("value", EncryptedString(512)))
    return md, t


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'enc.db'}")


def test_round_trip_and_ciphertext_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_SECRETS_KEY", KEY)
    md, t = _table()
    engine = _engine(tmp_path)
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(id=1, value="daraja-passkey-123"))
    with engine.connect() as conn:
        assert conn.execute(select(t.c.value)).scalar() == "daraja-passkey-123"
        raw = conn.exec_driver_sql("SELECT value FROM secrets").scalar()
    assert raw != "daraja-passkey-123"
    assert "daraja" not in raw


def test_none_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_SECRETS_KEY", KEY)
    md, t = _table()
    engine = _engine(tmp_path)
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(id=1, value=None))
    with engine.connect() as conn:
        assert conn.execute(select(t.c.value)).scalar() is None


def test_missing_key_refuses_to_store_plaintext(tmp_path, monkeypatch):
    monkeypatch.delenv("TENANT_SECRETS_KEY", raising=False)
    md, t = _table()
    engine = _engine(tmp_path)
    md.create_all(engine)
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as conn:
            conn.execute(t.insert().values(id=1, value="secret"))
    assert isinstance(excinfo.value, SecretsKeyMissing) or \
        isinstance(getattr(excinfo.value, "orig", None), SecretsKeyMissing) or \
        "SecretsKeyMissing" in repr(excinfo.value)


def test_none_needs_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TENANT_SECRETS_KEY", raising=False)
    md, t = _table()
    engine = _engine(tmp_path)
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(id=1, value=None))


def test_wrong_key_does_not_return_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_SECRETS_KEY", KEY)
    md, t = _table()
    engine = _engine(tmp_path)
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(id=1, value="secret"))
    monkeypatch.setenv("TENANT_SECRETS_KEY", Fernet.generate_key().decode())
    with pytest.raises(Exception):
        with engine.connect() as conn:
            conn.execute(select(t.c.value)).scalar()
