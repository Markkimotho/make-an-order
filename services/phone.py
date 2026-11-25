"""Kenyan mobile number normalisation.

Every phone stored by the app is ``254XXXXXXXXX``: country code, then a
9-digit subscriber number starting with 7 (07xx) or 1 (010x / 011x). The same
customer typed as ``0712 345 678``, ``+254712345678`` or ``712345678`` must
land on one value, or per-tenant unique constraints and SMS dedupe miss.
"""
import re

_SUBSCRIBER = re.compile(r"^[71]\d{8}$")


class InvalidPhone(ValueError):
    """Not a Kenyan mobile number."""


def normalize_ke_phone(raw):
    """Return ``254XXXXXXXXX`` or raise :class:`InvalidPhone`.

    Accepts ``07..``/``01..`` (10 digits), ``7..``/``1..`` (9 digits),
    ``254..`` and ``+254..`` (12 digits), with any spaces, dashes, dots or
    brackets.
    """
    if raw is None:
        raise InvalidPhone("phone is required")
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("254"):
        subscriber = digits[3:]
    elif len(digits) == 10 and digits.startswith("0"):
        subscriber = digits[1:]
    elif len(digits) == 9:
        subscriber = digits
    else:
        raise InvalidPhone(f"not a Kenyan mobile number: {raw!r}")
    if not _SUBSCRIBER.match(subscriber):
        raise InvalidPhone(f"not a Kenyan mobile number: {raw!r}")
    return "254" + subscriber
