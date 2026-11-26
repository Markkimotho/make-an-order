"""WhatsApp Cloud API (Meta), with one business's own number and token.

Only what the bot needs: send a text, reply buttons or a list; mark a
message read; read the number's details to check the connection; and check
the signature Meta puts on every webhook.

Messages are built here from a small neutral shape (see ``payload``) so the
bot never deals with Meta's JSON, and every length limit is applied in one
place: a title over Meta's limit is refused by Meta, not cut, so we cut it.
"""
import hashlib
import hmac
import logging

import requests

from config import Config

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
TIMEOUT = 10

# Meta's limits (characters).
TEXT_MAX = 4096
INTERACTIVE_BODY_MAX = 1024
BUTTON_TITLE_MAX = 20
MAX_BUTTONS = 3
LIST_BUTTON_MAX = 20
ROW_TITLE_MAX = 24
ROW_DESC_MAX = 72
SECTION_TITLE_MAX = 24
MAX_ROWS = 10
HEADER_MAX = 60
ID_MAX = 200

# Meta error code: the customer's 24-hour window is closed, so only an
# approved template may be sent.
OUTSIDE_WINDOW = 131047


class WhatsAppError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class WhatsAppNotConfigured(WhatsAppError):
    """The business has not connected its WhatsApp number."""


def cut(text, limit):
    text = " ".join(str(text or "").split()) if limit < 100 else str(text or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def payload(to, message):
    """Meta's JSON for one neutral message:

    {"type": "text", "body": str}
    {"type": "buttons", "body": str, "buttons": [(id, title), ...]}     at most 3
    {"type": "list", "body": str, "button": str, "rows": [(id, title, description)],
     "section": str, "header": str | None}                           at most 10 rows
    """
    base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    kind = message["type"]
    if kind == "text":
        return {**base, "type": "text",
                "text": {"preview_url": True, "body": cut(message["body"], TEXT_MAX)}}
    if kind == "buttons":
        buttons = message["buttons"][:MAX_BUTTONS]
        return {**base, "type": "interactive", "interactive": {
            "type": "button",
            "body": {"text": cut(message["body"], INTERACTIVE_BODY_MAX)},
            "action": {"buttons": [{"type": "reply",
                                    "reply": {"id": bid[:ID_MAX],
                                              "title": cut(title, BUTTON_TITLE_MAX)}}
                                   for bid, title in buttons]}}}
    if kind == "list":
        rows = [{"id": rid[:ID_MAX], "title": cut(title, ROW_TITLE_MAX),
                 **({"description": cut(desc, ROW_DESC_MAX)} if desc else {})}
                for rid, title, desc in message["rows"][:MAX_ROWS]]
        interactive = {
            "type": "list",
            "body": {"text": cut(message["body"], INTERACTIVE_BODY_MAX)},
            "action": {"button": cut(message.get("button") or "Choose", LIST_BUTTON_MAX),
                       "sections": [{"title": cut(message.get("section") or "Options",
                                                  SECTION_TITLE_MAX),
                                     "rows": rows}]}}
        if message.get("header"):
            interactive["header"] = {"type": "text", "text": cut(message["header"], HEADER_MAX)}
        return {**base, "type": "interactive", "interactive": interactive}
    raise ValueError(f"unknown message type {kind}")


def readable(message):
    """What a person would read: kept in the message log and shown to staff."""
    kind = message["type"]
    if kind == "text":
        return message["body"]
    if kind == "buttons":
        return message["body"] + "\n" + "  ".join(f"[{t}]" for _, t in message["buttons"])
    rows = "\n".join(f"• {t}" + (f" ({d})" if d else "") for _, t, d in message["rows"])
    return message["body"] + "\n" + rows


def verify_signature(app_secret, raw_body, header):
    """Meta signs each webhook body with the app secret:
    ``X-Hub-Signature-256: sha256=<hex hmac>``."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


class WhatsAppClient:
    def __init__(self, tenant):
        token = tenant.wa_access_token
        if not tenant.wa_phone_number_id or not token:
            raise WhatsAppNotConfigured("WhatsApp is not connected")
        self.phone_number_id = tenant.wa_phone_number_id
        self._headers = {"Authorization": f"Bearer {token}"}

    def _url(self, path):
        return f"{GRAPH}/{Config.WHATSAPP_GRAPH_VERSION}/{path}"

    @staticmethod
    def _error(response):
        try:
            err = response.json().get("error") or {}
        except ValueError:
            err = {}
        detail = (err.get("error_data") or {}).get("details") or err.get("message")
        return WhatsAppError((detail or f"HTTP {response.status_code}")[:255], err.get("code"))

    def send(self, to, message):
        """Send one neutral message. Returns Meta's message id."""
        try:
            r = requests.post(self._url(f"{self.phone_number_id}/messages"),
                              json=payload(to, message), headers=self._headers,
                              timeout=TIMEOUT)
        except requests.RequestException as e:
            raise WhatsAppError(f"WhatsApp could not be reached: {e}"[:255])
        if r.status_code >= 400:
            raise self._error(r)
        try:
            return r.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise WhatsAppError("WhatsApp gave no message id")

    def mark_read(self, wa_message_id):
        """Blue ticks on the customer's message. Never fails the reply."""
        try:
            requests.post(self._url(f"{self.phone_number_id}/messages"),
                          json={"messaging_product": "whatsapp", "status": "read",
                                "message_id": wa_message_id},
                          headers=self._headers, timeout=TIMEOUT)
        except requests.RequestException:
            logger.info("mark read failed for %s", wa_message_id)

    def number_details(self):
        """{display_phone_number, verified_name, quality_rating}: proves the id
        and token belong together."""
        try:
            r = requests.get(self._url(self.phone_number_id),
                             params={"fields": "display_phone_number,verified_name,"
                                               "quality_rating"},
                             headers=self._headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise WhatsAppError(f"WhatsApp could not be reached: {e}"[:255])
        if r.status_code >= 400:
            raise self._error(r)
        return r.json()
