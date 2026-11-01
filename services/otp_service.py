"""Manager-override OTP store.

Used when a cashier needs an admin to authorise a sensitive action (void
after completion, discount above threshold, refund, etc.). The override
flow is:

1. Cashier calls an endpoint that generates a 6-digit OTP, stored here with a
   TTL, and SMS'd to the manager via :mod:`services.sms_service`.
2. Cashier enters the OTP they receive from the manager. The endpoint calls
   :func:`verify_otp` to consume it; on success the underlying action proceeds.

Backed by Redis when ``REDIS_URL`` is reachable; otherwise falls back to a
process-local dict so the app keeps working in dev / single-worker setups.
A warning is logged on first fallback so this isn't silently used in prod.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)

_KEY_PREFIX = "make-an-order:otp:"
_redis_client = None
_redis_checked = False
_warned_fallback = False

# In-memory fallback: {key: (otp, expires_at_epoch)}
_memory_store: dict[str, tuple[str, float]] = {}
_memory_lock = threading.Lock()


def _get_redis():
    """Return a connected Redis client, or None if unavailable."""
    global _redis_client, _redis_checked, _warned_fallback
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True,
                                      socket_connect_timeout=2)
        client.ping()
        _redis_client = client
        logger.info("OTP store: using Redis at %s", Config.REDIS_URL)
    except Exception as e:
        if not _warned_fallback:
            logger.warning(
                "OTP store: Redis unavailable (%s) — falling back to in-memory store. "
                "DO NOT USE IN MULTI-WORKER PRODUCTION.", e,
            )
            _warned_fallback = True
        _redis_client = None
    return _redis_client


def _purge_expired() -> None:
    now = time.time()
    with _memory_lock:
        expired = [k for k, (_, exp) in _memory_store.items() if exp <= now]
        for k in expired:
            _memory_store.pop(k, None)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def generate_otp(scope: str, length: int = 6) -> str:
    """Generate, store, and return a numeric OTP for ``scope``.

    ``scope`` is any caller-defined string — typically ``f"override:{user_id}"``
    or ``f"void:{transaction_id}"``. Using the same scope twice overwrites
    the previous OTP.
    """
    if length < 4 or length > 10:
        raise ValueError("length must be between 4 and 10")
    otp = "".join(str(secrets.randbelow(10)) for _ in range(length))
    ttl = int(Config.MANAGER_OTP_TTL or 300)
    key = _KEY_PREFIX + scope

    client = _get_redis()
    if client is not None:
        try:
            client.setex(key, ttl, otp)
            return otp
        except Exception as e:
            logger.error("OTP store: Redis setex failed (%s) — using memory", e)

    with _memory_lock:
        _memory_store[key] = (otp, time.time() + ttl)
    return otp


def verify_otp(scope: str, candidate: str, *, consume: bool = True) -> bool:
    """Verify ``candidate`` against the OTP previously stored for ``scope``.

    When ``consume`` is True (default) a successful match deletes the OTP so
    it cannot be reused. Returns ``False`` on miss/expiry/mismatch.
    """
    if not candidate:
        return False
    key = _KEY_PREFIX + scope
    candidate = candidate.strip()

    client = _get_redis()
    if client is not None:
        try:
            stored = client.get(key)
            if stored and secrets_compare(stored, candidate):
                if consume:
                    client.delete(key)
                return True
            return False
        except Exception as e:
            logger.error("OTP store: Redis get failed (%s) — using memory", e)

    _purge_expired()
    with _memory_lock:
        entry = _memory_store.get(key)
        if not entry:
            return False
        stored, exp = entry
        if exp <= time.time():
            _memory_store.pop(key, None)
            return False
        if not secrets_compare(stored, candidate):
            return False
        if consume:
            _memory_store.pop(key, None)
        return True


def secrets_compare(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    import hmac
    return hmac.compare_digest(a, b)


def peek_otp(scope: str) -> Optional[str]:
    """Return the stored OTP for ``scope`` without consuming it (debug only)."""
    key = _KEY_PREFIX + scope
    client = _get_redis()
    if client is not None:
        try:
            return client.get(key)
        except Exception:
            return None
    _purge_expired()
    with _memory_lock:
        entry = _memory_store.get(key)
        return entry[0] if entry else None
