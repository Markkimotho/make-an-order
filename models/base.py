"""Database handle, naming convention, the Tenant model and TenantMixin."""
import secrets
from datetime import datetime, timedelta

from flask_sqlalchemy import SQLAlchemy  # type: ignore
from flask_sqlalchemy.session import Session as FlaskSession  # type: ignore
from sqlalchemy import MetaData
from sqlalchemy.orm import declared_attr, deferred, validates

from models.types import EncryptedString

# Every constraint and index gets a deterministic name. Alembic batch mode
# (SQLite table rebuilds) cannot drop or alter an unnamed constraint, and
# later migrations look constraints up by these names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class TenantSession(FlaskSession):
    """The app's session. merge() is checked after the fact, because
    SQLAlchemy attaches the merged copy before filling in its attributes,
    so the before_attach hook cannot see whose row it is."""

    def merge(self, instance, *args, **kwargs):
        merged = super().merge(instance, *args, **kwargs)
        from auth.tenancy import check_merged
        check_merged(self, merged)
        return merged


db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION),
                session_options={"class_": TenantSession})

VERTICALS = ("pharmacy", "distributor", "institution")
SUBSCRIPTION_STATUSES = ("trial", "active", "past_due", "cancelled")
SHORTCODE_TYPES = ("till", "paybill")
DARAJA_ENVS = ("sandbox", "production")
TRIAL_DAYS = 14


def _new_callback_token():
    return secrets.token_urlsafe(24)


def _trial_end():
    return datetime.utcnow() + timedelta(days=TRIAL_DAYS)


class Tenant(db.Model):
    """One business. The only table without a tenant_id.

    Secret columns are Fernet-encrypted at rest (``*_encrypted`` in the
    database) and never appear in :meth:`to_dict`.
    """
    __tablename__ = "tenants"
    __table_args__ = (
        db.CheckConstraint(f"vertical IN {VERTICALS!r}", name="vertical"),
    )

    id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name                = db.Column(db.String(255), nullable=False)
    slug                = db.Column(db.String(80), unique=True, nullable=False)
    vertical            = db.Column(db.String(20), nullable=False)

    kra_pin             = db.Column(db.String(20), nullable=True)
    # Secrets load only when read (deferred), so ordinary tenant loads never decrypt.
    etims_token         = deferred(db.Column("etims_token_encrypted", EncryptedString(1024), nullable=True))

    daraja_shortcode        = db.Column(db.String(20), nullable=True)
    daraja_shortcode_type   = db.Column(db.String(10), nullable=True)   # till | paybill
    daraja_till_number      = db.Column(db.String(20), nullable=True)
    daraja_consumer_key     = deferred(db.Column("daraja_consumer_key_encrypted", EncryptedString(512), nullable=True))
    daraja_consumer_secret  = deferred(db.Column("daraja_consumer_secret_encrypted", EncryptedString(512), nullable=True))
    daraja_passkey          = deferred(db.Column("daraja_passkey_encrypted", EncryptedString(512), nullable=True))
    daraja_env              = db.Column(db.String(12), nullable=False, default="sandbox")
    callback_token          = db.Column(db.String(64), nullable=False, default=_new_callback_token)

    at_sender_id        = db.Column(db.String(20), nullable=True)
    subscription_status = db.Column(db.String(20), nullable=False, default="trial")
    trial_ends_at       = db.Column(db.DateTime, nullable=True, default=_trial_end)

    ppb_premises_license        = db.Column(db.String(64), nullable=True)
    ppb_premises_license_expiry = db.Column(db.Date, nullable=True)
    superintendent_reg_no       = db.Column(db.String(64), nullable=True)

    created_at          = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    @validates("vertical")
    def _check_vertical(self, key, value):
        if value not in VERTICALS:
            raise ValueError(f"vertical must be one of {VERTICALS}")
        return value

    @validates("subscription_status")
    def _check_status(self, key, value):
        if value not in SUBSCRIPTION_STATUSES:
            raise ValueError(f"subscription_status must be one of {SUBSCRIPTION_STATUSES}")
        return value

    @validates("daraja_shortcode_type")
    def _check_shortcode_type(self, key, value):
        if value is not None and value not in SHORTCODE_TYPES:
            raise ValueError(f"daraja_shortcode_type must be one of {SHORTCODE_TYPES}")
        return value

    def is_active_subscription(self, now=None):
        """True for a paying tenant or a trial that has not ended."""
        if self.subscription_status == "active":
            return True
        if self.subscription_status == "trial":
            now = now or datetime.utcnow()
            return self.trial_ends_at is None or self.trial_ends_at > now
        return False

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "vertical": self.vertical,
            "kra_pin": self.kra_pin,
            "daraja_shortcode": self.daraja_shortcode,
            "daraja_shortcode_type": self.daraja_shortcode_type,
            "daraja_env": self.daraja_env,
            "at_sender_id": self.at_sender_id,
            "subscription_status": self.subscription_status,
            "trial_ends_at": self.trial_ends_at.isoformat() if self.trial_ends_at else None,
            "ppb_premises_license": self.ppb_premises_license,
            "superintendent_reg_no": self.superintendent_reg_no,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TenantMixin:
    """Marks a model as tenant-owned. auth/tenancy.py scopes every query on
    it to the bound tenant and stamps tenant_id on insert."""

    @declared_attr
    def tenant_id(cls):
        return db.Column(db.Integer, db.ForeignKey("tenants.id", ondelete="CASCADE"),
                         nullable=False, index=True)
