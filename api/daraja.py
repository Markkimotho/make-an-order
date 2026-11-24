"""Public Daraja webhooks, one set of URLs per business.

URL prefix ``/api/payments/daraja``:

  POST /stk/<slug>/<token>            STK push result
  POST /c2b/<slug>/<token>            Paybill/Till confirmation
  POST /c2b/<slug>/<token>/validate   Paybill validation (never rejects)

The slug says which business; the token (the tenant's callback_token, a
secret only Safaricom and the business know) proves it came through the
registered URL. A wrong slug or token gets 404 and nothing is recorded.
Accepted callbacks always get HTTP 200 with the exact body Daraja expects,
including duplicates and unmatched payments; anything else makes Daraja
retry. Paths never contain "mpesa" or "safaricom" (Daraja refuses those).
"""
import hmac
import logging

from flask import Blueprint, current_app, jsonify, request

from auth.tenancy import tenant_scope, unscoped
from models import Tenant, db
from services.billing import stk

daraja_bp = Blueprint("daraja_webhooks", __name__)
logger = logging.getLogger(__name__)

STK_ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}
C2B_CONFIRMED = {"ResultCode": 0, "ResultDesc": "Confirmation received successfully"}
C2B_VALIDATED = {"ResultCode": 0, "ResultDesc": "Accepted"}


def _tenant_for(slug, token):
    """The tenant id if slug and token match, else None (constant-time)."""
    with unscoped():
        row = db.session.query(Tenant.id, Tenant.callback_token).filter_by(slug=slug).first()
    if row is None:
        hmac.compare_digest(token, "x" * len(token))   # same work either way
        return None
    return row.id if hmac.compare_digest(token, row.callback_token) else None


def _source_allowed():
    allowed = current_app.config.get("DARAJA_ALLOWED_IPS") or ""
    ips = {ip.strip() for ip in allowed.split(",") if ip.strip()}
    return not ips or request.remote_addr in ips


def _refused():
    return jsonify({"error": "Not found"}), 404


@daraja_bp.route("/stk/<slug>/<token>", methods=["POST"])
def stk_result(slug, token):
    tenant_id = _tenant_for(slug, token)
    if tenant_id is None or not _source_allowed():
        logger.warning("Refused STK callback for %s from %s", slug, request.remote_addr)
        return _refused()
    payload = request.get_json(silent=True) or {}
    with tenant_scope(tenant_id):
        stk.handle_stk_callback(payload)
    return jsonify(STK_ACCEPTED), 200


@daraja_bp.route("/c2b/<slug>/<token>", methods=["POST"])
def c2b_confirmation(slug, token):
    tenant_id = _tenant_for(slug, token)
    if tenant_id is None or not _source_allowed():
        logger.warning("Refused C2B confirmation for %s from %s", slug, request.remote_addr)
        return _refused()
    payload = request.get_json(silent=True) or {}
    if not payload.get("TransID") or payload.get("TransAmount") in (None, ""):
        logger.warning("C2B confirmation for %s without TransID/TransAmount: ignored", slug)
        return jsonify(C2B_CONFIRMED), 200
    with tenant_scope(tenant_id):
        stk.handle_c2b(payload)
    return jsonify(C2B_CONFIRMED), 200


@daraja_bp.route("/c2b/<slug>/<token>/validate", methods=["POST"])
def c2b_validation(slug, token):
    """Never rejects: an unknown account reference is accepted and lands in
    the review queue, because turning away a customer's money is worse than
    matching it by hand."""
    if _tenant_for(slug, token) is None or not _source_allowed():
        return _refused()
    return jsonify(C2B_VALIDATED), 200
