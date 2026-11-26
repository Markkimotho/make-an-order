"""Meta's WhatsApp webhook, one URL per business: /webhooks/whatsapp/<slug>.

GET  Meta's subscription check: answers ``hub.challenge`` only when
     ``hub.verify_token`` is the business's own verify token (settings page).
POST Messages and delivery statuses. The body must carry Meta's signature
     (``X-Hub-Signature-256``, HMAC-SHA256 of the raw body with the business's
     app secret), or it is refused with 403 and nothing is read.

A signed delivery gets 200 once every message in it is saved. If one could
not be handled (a database error, a deadlock), the answer is 500 and Meta
delivers the batch again; messages already saved are dropped as duplicates.
"""
import hmac
import logging

from flask import Blueprint, abort, g, jsonify, request

from auth.tenancy import tenant_scope
from models import Tenant, db
from models.types import SecretsKeyMissing
from public.limits import limiter
from public.routes import _business
from services.whatsapp import service
from services.whatsapp.client import verify_signature

whatsapp_webhook_bp = Blueprint("whatsapp_webhook", __name__)
logger = logging.getLogger(__name__)


def _tenant_id(slug):
    found = _business(slug)
    if found is None:
        abort(404)
    return found[0]


@whatsapp_webhook_bp.route("/<slug>", methods=["GET"])
@limiter.limit("30 per minute")
def verify(slug):
    tenant_id = _tenant_id(slug)
    with tenant_scope(tenant_id):
        tenant = db.session.get(Tenant, tenant_id)
        expected = tenant.wa_verify_token or ""
    given = request.args.get("hub.verify_token") or ""
    if (request.args.get("hub.mode") != "subscribe" or not expected
            or not hmac.compare_digest(given, expected)):
        logger.warning("WhatsApp webhook verification refused for %s", slug)
        return jsonify({"error": "Forbidden"}), 403
    return (request.args.get("hub.challenge") or ""), 200, {"Content-Type": "text/plain"}


@whatsapp_webhook_bp.route("/<slug>", methods=["POST"])
@limiter.limit("600 per minute")
def receive(slug):
    tenant_id = _tenant_id(slug)
    raw = request.get_data(cache=True)
    with tenant_scope(tenant_id):
        tenant = db.session.get(Tenant, tenant_id)
        try:
            secret = tenant.wa_app_secret
        except SecretsKeyMissing:
            secret = None
        if not verify_signature(secret, raw, request.headers.get("X-Hub-Signature-256")):
            logger.warning("WhatsApp webhook with a bad signature for %s from %s",
                           slug, request.remote_addr)
            return jsonify({"error": "Forbidden"}), 403
        body = request.get_json(silent=True) or {}
        try:
            counts = service.handle_webhook(tenant, body)
            logger.info("whatsapp webhook %s: %s", slug, counts)
        except Exception as e:
            # A message that was not saved: 500 makes Meta deliver it again.
            # Messages already handled are dropped as duplicates on the retry.
            db.session.rollback()
            logger.error("whatsapp webhook for %s needs a retry: %s", slug, e)
            return jsonify({"error": "retry"}), 500
    return jsonify({"ok": True}), 200
