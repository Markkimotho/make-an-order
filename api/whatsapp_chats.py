"""Staff side of the WhatsApp chatbot: chats a customer handed to a person,
read and answered from the app (URL prefix /whatsapp).

  GET  /chats                 chats, the ones waiting for a person first
  GET  /chats/waiting         how many wait (the nav badge)
  GET  /chats/<id>            one chat and its last messages
  POST /chats/<id>/reply      {body}: answer on WhatsApp (within 24 hours of the
                              customer's last message: Meta's rule)
  POST /chats/<id>/hand-back  the bot answers this customer again

Anyone on the till (cashier, pharmacist, admin, owner) may answer.
"""
from flask import Blueprint, g, jsonify, request, session

from auth.auth_middleware import cashier_api_required
from models import Tenant, WhatsAppChat, WhatsAppMessage, db
from services import audit_service
from services.whatsapp import service

whatsapp_chats_bp = Blueprint("whatsapp_chats", __name__)

HISTORY = 100


def _row(chat, last=None):
    out = chat.to_dict()
    out["phone"] = service.local_phone(chat.wa_id) or f"+{chat.wa_id}"
    out["can_reply"] = service.in_window(chat)
    if last is not None:
        out["last"] = {"direction": last.direction, "body": last.body[:140],
                       "created_at": last.created_at.isoformat()}
    return out


def _chat_or_404(chat_id):
    chat = db.session.get(WhatsAppChat, chat_id)
    if chat is None:
        return None, (jsonify({"error": "Chat not found"}), 404)
    return chat, None


@whatsapp_chats_bp.route("/chats", methods=["GET"])
@cashier_api_required
def list_chats():
    show = request.args.get("show", "all")
    q = WhatsAppChat.query
    if show == "waiting":
        q = q.filter(WhatsAppChat.needs_reply.is_(True))
    chats = (q.order_by(WhatsAppChat.needs_reply.desc(),
                        WhatsAppChat.last_inbound_at.desc(), WhatsAppChat.id.desc())
             .limit(100).all())
    rows = []
    for chat in chats:
        last = (WhatsAppMessage.query.filter_by(chat_id=chat.id)
                .order_by(WhatsAppMessage.id.desc()).first())
        rows.append(_row(chat, last))
    return jsonify({"chats": rows, "waiting": _waiting()}), 200


def _waiting():
    return WhatsAppChat.query.filter(WhatsAppChat.needs_reply.is_(True)).count()


@whatsapp_chats_bp.route("/chats/waiting", methods=["GET"])
@cashier_api_required
def waiting():
    return jsonify({"count": _waiting()}), 200


@whatsapp_chats_bp.route("/chats/<int:chat_id>", methods=["GET"])
@cashier_api_required
def get_chat(chat_id):
    chat, error = _chat_or_404(chat_id)
    if error:
        return error
    messages = (WhatsAppMessage.query.filter_by(chat_id=chat.id)
                .order_by(WhatsAppMessage.id.desc()).limit(HISTORY).all())
    return jsonify({"chat": _row(chat),
                    "messages": [m.to_dict() for m in reversed(messages)]}), 200


@whatsapp_chats_bp.route("/chats/<int:chat_id>/reply", methods=["POST"])
@cashier_api_required
def reply(chat_id):
    chat, error = _chat_or_404(chat_id)
    if error:
        return error
    body = str((request.get_json(silent=True) or {}).get("body") or "")
    tenant = db.session.get(Tenant, g.current_tenant.id)
    if not service.connected(tenant):
        return jsonify({"error": "WhatsApp is not connected. An owner can connect it in "
                                 "Settings."}), 409
    try:
        row = service.staff_reply(tenant, chat, body, session.get("user_id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except service.WindowClosed as e:
        return jsonify({"error": str(e)}), 409
    audit_service.log(session.get("user_id"), "whatsapp.reply", "WhatsAppChat", chat_id, None,
                      {"status": row.status})
    db.session.commit()
    if row.status == "failed":
        return jsonify({"error": f"WhatsApp did not take the message: {row.error}",
                        "message": row.to_dict()}), 502
    return jsonify({"message": row.to_dict()}), 201


@whatsapp_chats_bp.route("/chats/<int:chat_id>/hand-back", methods=["POST"])
@cashier_api_required
def hand_back(chat_id):
    chat, error = _chat_or_404(chat_id)
    if error:
        return error
    service.hand_back(chat)
    audit_service.log(session.get("user_id"), "whatsapp.hand_back", "WhatsAppChat", chat_id)
    db.session.commit()
    return jsonify({"chat": _row(chat)}), 200
