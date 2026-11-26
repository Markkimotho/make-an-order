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
from sqlalchemy.exc import IntegrityError

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
        "wa": _whatsapp_context(tenant),
    }


def _whatsapp_context(tenant):
    """The chatbot's connection, for businesses with the online shop."""
    from services import online_shop
    from verticals.features import features_for
    if "online_shop" not in features_for(tenant.vertical):
        return None
    state = {}
    for field in ("wa_access_token", "wa_app_secret"):
        try:
            state[field] = bool(getattr(tenant, field))
        except SecretsKeyMissing:
            state[field] = True
    # A webhook, like Daraja's callbacks: the server's public address
    # (APP_URL), else the shop's link base.
    from config import Config
    base = (Config.APP_URL or "").rstrip("/") or online_shop.absolute("")
    return {"webhook_url": f"{base}/webhooks/whatsapp/{tenant.slug}",
            "verify_token": tenant.wa_verify_token, "token_set": state["wa_access_token"],
            "secret_set": state["wa_app_secret"]}


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


# --- WhatsApp chatbot ------------------------------------------------------------

WA_ID = re.compile(r"^\d{6,40}$")


def _wa_feature(tenant):
    from verticals.features import features_for
    return "online_shop" in features_for(tenant.vertical)


@settings_bp.route("/whatsapp", methods=["POST"])
@superuser_required
def save_whatsapp():
    """The business's own WhatsApp Cloud API number. The token and app secret
    are write-only; a blank field keeps what is stored. The bot can only be
    switched on once the number id, token and app secret are all set."""
    tenant = _tenant()
    if not _wa_feature(tenant):
        return redirect(url_for("settings.page"))
    form = request.form
    errors = []
    number_id = _clean(form, "wa_phone_number_id")
    account_id = _clean(form, "wa_business_account_id")
    token = (form.get("wa_access_token") or "").strip()
    app_secret = (form.get("wa_app_secret") or "").strip()
    enable = form.get("wa_bot_enabled") == "on"
    if number_id and not WA_ID.match(number_id):
        errors.append("Phone number ID is the long number from Meta's WhatsApp API setup page "
                      "(digits only), not the phone number.")
    if account_id and not WA_ID.match(account_id):
        errors.append("WhatsApp Business account ID must be digits only.")
    if (token or app_secret) and not secrets_key_configured():
        errors.append("The server has no TENANT_SECRETS_KEY, so the WhatsApp token cannot be "
                      "saved. Ask the platform administrator.")
    if number_id and Tenant.query.filter(Tenant.wa_phone_number_id == number_id,
                                         Tenant.id != tenant.id).first() is not None:
        errors.append("That WhatsApp phone number ID is already connected to another business.")
    if errors:
        return render_template("settings.html", **_context(tenant, errors, form)), 400

    changed_number = number_id != tenant.wa_phone_number_id
    tenant.wa_phone_number_id = number_id
    tenant.wa_business_account_id = account_id
    if token:
        tenant.wa_access_token = token
    if app_secret:
        tenant.wa_app_secret = app_secret
    if changed_number:
        tenant.wa_display_phone = None
    if not tenant.wa_verify_token:
        tenant.wa_verify_token = secrets.token_urlsafe(24)
    ready = bool(tenant.wa_phone_number_id and tenant.wa_access_token and tenant.wa_app_secret)
    if enable and not ready:
        errors.append("Enter the phone number ID, access token and app secret before "
                      "switching the bot on.")
    tenant.wa_bot_enabled = enable and ready
    audit_service.log(session.get("user_id"), "tenant.whatsapp", "Tenant", tenant.id, None,
                      {"wa_phone_number_id": number_id, "token_changed": bool(token),
                       "secret_changed": bool(app_secret), "bot": bool(tenant.wa_bot_enabled)})
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        errors = ["That WhatsApp phone number ID is already connected to another business."]
        return render_template("settings.html", **_context(_tenant(), errors, form)), 400
    if errors:
        return render_template("settings.html", **_context(_tenant(), errors, form)), 400
    flash("WhatsApp settings saved." + (" The bot is on." if tenant.wa_bot_enabled else ""),
          "success")
    return redirect(url_for("settings.page") + "#whatsapp")


@settings_bp.route("/whatsapp/check", methods=["POST"])
@superuser_required
def check_whatsapp():
    """Ask Meta for the number's details: proves the id and token match."""
    from services.whatsapp.client import WhatsAppClient, WhatsAppError
    tenant = _tenant()
    try:
        details = WhatsAppClient(tenant).number_details()
    except (WhatsAppError, SecretsKeyMissing) as e:
        flash(f"WhatsApp did not accept the connection: {e}", "danger")
        return redirect(url_for("settings.page") + "#whatsapp")
    tenant.wa_display_phone = str(details.get("display_phone_number") or "")[:32] or None
    db.session.commit()
    flash(f"Connected to {details.get('verified_name') or 'your business'} "
          f"({tenant.wa_display_phone or 'number'}).", "success")
    return redirect(url_for("settings.page") + "#whatsapp")


@settings_bp.route("/whatsapp/test", methods=["POST"])
@superuser_required
def test_whatsapp():
    """Send Meta's hello_world template to a number: proves the number id and
    token can send, before any customer has written in."""
    from services.whatsapp.client import WhatsAppClient, WhatsAppError
    tenant = _tenant()
    to = re.sub(r"\D", "", request.form.get("to") or "")
    if to.startswith("0") and len(to) == 10:
        to = "254" + to[1:]
    if not 8 <= len(to) <= 15:
        flash("Enter the phone number to send the test to, with its country code "
              "(for example 254712345678).", "danger")
        return redirect(url_for("settings.page") + "#whatsapp")
    try:
        WhatsAppClient(tenant).send_template(to)
    except (WhatsAppError, SecretsKeyMissing) as e:
        flash(f"WhatsApp did not send the test: {e}", "danger")
        return redirect(url_for("settings.page") + "#whatsapp")
    audit_service.log(session.get("user_id"), "tenant.whatsapp_test", "Tenant", tenant.id)
    db.session.commit()
    flash("Test message sent. It should arrive on that phone within a minute. "
          "Reply \"Hi\" from it to talk to the bot.", "success")
    return redirect(url_for("settings.page") + "#whatsapp")


@settings_bp.route("/whatsapp/verify-token", methods=["POST"])
@superuser_required
def rotate_whatsapp_verify_token():
    tenant = _tenant()
    tenant.wa_verify_token = secrets.token_urlsafe(24)
    audit_service.log(session.get("user_id"), "tenant.whatsapp_verify_token", "Tenant", tenant.id)
    db.session.commit()
    flash("New verify token. Enter it in Meta's webhook settings and verify again.", "success")
    return redirect(url_for("settings.page") + "#whatsapp")


@settings_bp.route("/whatsapp/disconnect", methods=["POST"])
@superuser_required
def disconnect_whatsapp():
    tenant = _tenant()
    tenant.wa_bot_enabled = False
    tenant.wa_phone_number_id = None
    tenant.wa_business_account_id = None
    tenant.wa_access_token = None
    tenant.wa_app_secret = None
    tenant.wa_display_phone = None
    audit_service.log(session.get("user_id"), "tenant.whatsapp_disconnect", "Tenant", tenant.id)
    db.session.commit()
    flash("WhatsApp disconnected. The bot no longer answers.", "success")
    return redirect(url_for("settings.page") + "#whatsapp")
