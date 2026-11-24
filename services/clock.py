"""Dates as Kenyan businesses see them.

Servers (Heroku) run on UTC, three hours behind Nairobi: between 00:00 and
02:59 EAT, UTC's date is still yesterday. Expiry, aging and the daily SMS
jobs use these helpers, never ``date.today()``.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NAIROBI = ZoneInfo("Africa/Nairobi")


def now_nairobi(now=None):
    """``now`` (a naive UTC datetime, as the app stores them) in Nairobi."""
    now = now or datetime.utcnow()
    return now.replace(tzinfo=timezone.utc).astimezone(NAIROBI)


def today_nairobi(now=None):
    return now_nairobi(now).date()
