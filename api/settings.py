"""Business settings (owner only): M-Pesa, SMS sender, KRA and PPB details,
and the online shop (open or closed, delivery, WhatsApp number, share link).

Secrets (Daraja consumer key, secret, passkey) are write-only: the page
shows whether each is set, never its value; a blank field keeps what is
stored. Saving them needs TENANT_SECRETS_KEY on the server.
"""
import logging
import re
import secrets
from datetime import datetime

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from auth.auth_middleware import superuser_required
from models import DARAJA_ENVS, SHORTCODE_TYPES, SecretsKeyMissing, Tenant, db
from models.types import secrets_key_configured
from services import audit_service
from services.daraja import DarajaClient, DarajaError, callback_url, tokens
from services.phone import InvalidPhone, normalize_ke_phone

settings_bp = Blueprint("settings", __name__)
logger = logging.getLogger(__name__)

SECRET_FIELDS = (("daraja_consumer_key", "Consumer key"),
                 ("daraja_consumer_secret", "Consumer secret"),
                 ("daraja_passkey", "Passkey"))
KRA_PIN = re.compile(r"^[AP]\d{9}[A-Z]$")


def _tenant():
    return db.session.get(Tenant, g.current_tenant.id)


def _context(tenant, errors=None, form=None):
    from app import get_user_context
    secret_state = {}
    for field, _ in SECRET_FIELDS:
        try:
            secret_state[field] = bool(getattr(tenant, field))
        except SecretsKeyMissing:
            secret_state[field] = True          # stored but unreadable here: say so
    return {
        **get_user_context(),
        "t": tenant,
        "secret_fields": SECRET_FIELDS,
        "secret_state": secret_state,
        "stk_url": callback_url(tenant, "stk"),
        "c2b_url": callback_url(tenant, "c2b"),
        "errors": errors or [],
        "form": form or {},
        "shortcode_types": SHORTCODE_TYPES,
        "daraja_envs": DARAJA_ENVS,
        **_shop_context(tenant),
    }


def _shop_context(tenant):
    """The shop link, its QR code and the share links, for businesses with
    the online shop."""
    from services import online_shop
    from verticals.features import features_for
    if "online_shop" not in features_for(tenant.vertical):
        return {"shop": None}
    url = online_shop.shop_url(tenant)
    return {"shop": {"url": url, "qr_svg": online_shop.qr_svg(url),
                     "share_url": online_shop.wa_link(
                         None, f"Order from {tenant.name} online and pay with M-Pesa: {url}")}}


@settings_bp.route("/", methods=["GET"])
@superuser_required
def page():
    return render_template("settings.html", **_context(_tenant()))


def _clean(form, name, upper=False, max_len=None):
    value = (form.get(name) or "").strip()
    if upper:
        value = value.upper()
    if max_len:
        value = value[:max_len]
    return value or None


@settings_bp.route("/", methods=["POST"])
@superuser_required
def save():
    tenant = _tenant()
    form = request.form
    errors = []

    shortcode = _clean(form, "daraja_shortcode")
    kind = _clean(form, "daraja_shortcode_type")
    till = _clean(form, "daraja_till_number")
    env = _clean(form, "daraja_env") or "sandbox"
    sender = _clean(form, "at_sender_id", max_len=11)
    kra = _clean(form, "kra_pin", upper=True)
    ppb_expiry = _clean(form, "ppb_premises_license_expiry")

    if shortcode and not shortcode.isdigit():
        errors.append("Shortcode must be digits only.")
    if kind and kind not in SHORTCODE_TYPES:
        errors.append("Choose Till or Paybill.")
    if kind == "till" and not till:
        errors.append("A Till needs its till number as well as the store number.")
    if till and not till.isdigit():
        errors.append("Till number must be digits only.")
    if env not in DARAJA_ENVS:
        errors.append("Environment must be sandbox or production.")
    if kra and not KRA_PIN.match(kra):
        errors.append("KRA PIN looks wrong (expected like A123456789B).")
    expiry = None
    if ppb_expiry:
        try:
            expiry = datetime.strptime(ppb_expiry, "%Y-%m-%d").date()
        except ValueError:
            errors.append("PPB licence expiry must be a date.")
    whatsapp = _clean(form, "whatsapp_number")
    if whatsapp:
        try:
            whatsapp = normalize_ke_phone(whatsapp)
        except InvalidPhone:
            errors.append("WhatsApp number must be a Kenyan mobile number, like 0712 345 678.")
    if errors:
        return render_template("settings.html", **_context(tenant, errors, form)), 400

    before = {"daraja_shortcode": tenant.daraja_shortcode,
              "daraja_shortcode_type": tenant.daraja_shortcode_type,
              "daraja_env": tenant.daraja_env}
    tenant.daraja_shortcode = shortcode
    tenant.daraja_shortcode_type = kind
    tenant.daraja_till_number = till if kind == "till" else None
    tenant.daraja_env = env
    tenant.at_sender_id = sender
    tenant.kra_pin = kra
    tenant.ppb_premises_license = _clean(form, "ppb_premises_license", max_len=64)
    tenant.ppb_premises_license_expiry = expiry
    tenant.superintendent_reg_no = _clean(form, "superintendent_reg_no", max_len=64)
    tenant.manual_mpesa_enabled = form.get("manual_mpesa_enabled") == "on"
    if form.get("shop_settings") == "1":         # the shop section was on the form
        tenant.whatsapp_number = whatsapp
        tenant.shop_open = form.get("shop_open") == "on"
        tenant.shop_delivery = form.get("shop_delivery") == "on"

    new_secrets = {field: (form.get(field) or "").strip() for field, _ in SECRET_FIELDS}
    new_secrets = {k: v for k, v in new_secrets.items() if v}   # blank keeps the stored value
    if new_secrets and not secrets_key_configured():
        db.session.rollback()
        errors.append("The server has no TENANT_SECRETS_KEY, so M-Pesa keys cannot be saved. "
                      "Ask the platform administrator.")
        return render_template("settings.html", **_context(_tenant(), errors, form)), 500
    for field, value in new_secrets.items():
        setattr(tenant, field, value)
    changed_secrets = sorted(new_secrets)

    audit_service.log(session.get("user_id"), "tenant.settings", "Tenant", tenant.id, before,
                      {"daraja_shortcode": shortcode, "daraja_shortcode_type": kind,
                       "daraja_env": env, "secrets_changed": changed_secrets,
                       "shop_open": bool(tenant.shop_open),
                       "shop_delivery": bool(tenant.shop_delivery),
                       "whatsapp_number": tenant.whatsapp_number})
    db.session.commit()
    if changed_secrets or before["daraja_env"] != env:
        tokens.forget(tenant.id)
    flash("Settings saved.", "success")
    return redirect(url_for("settings.page"))


@settings_bp.route("/register-c2b", methods=["POST"])
@superuser_required
def register_c2b():
    tenant = _tenant()
    try:
        DarajaClient(tenant).register_c2b()
    except DarajaError as e:
        flash(f"Safaricom did not accept the URLs: {e}", "danger")
        return redirect(url_for("settings.page"))
    audit_service.log(session.get("user_id"), "tenant.register_c2b", "Tenant", tenant.id)
    db.session.commit()
    flash("Paybill confirmation URLs registered with Safaricom.", "success")
    return redirect(url_for("settings.page"))


@settings_bp.route("/rotate-token", methods=["POST"])
@superuser_required
def rotate_token():
    """New callback token. For a Paybill, Safaricom must accept the new URLs
    first, or confirmations would keep going to a URL we no longer accept;
    if it refuses, nothing changes. The old token keeps working for 24 hours
    so results for pushes already in flight still land."""
    tenant = _tenant()
    old = tenant.callback_token
    tenant.previous_callback_token = old
    tenant.callback_token = secrets.token_urlsafe(24)
    tenant.callback_token_rotated_at = datetime.utcnow()
    if tenant.daraja_shortcode_type == "paybill" and tenant.daraja_shortcode:
        try:
            DarajaClient(tenant).register_c2b()
        except DarajaError as e:
            db.session.rollback()
            flash(f"Safaricom did not accept the new URLs ({e}). The callback token was "
                  "not changed.", "danger")
            return redirect(url_for("settings.page"))
    audit_service.log(session.get("user_id"), "tenant.rotate_callback_token", "Tenant", tenant.id)
    db.session.commit()
    flash("Callback token changed. The old one keeps working for 24 hours.", "success")
    return redirect(url_for("settings.page"))
