"""Daraja OAuth tokens, cached per business.

Redis key ``daraja:token:<tenant_id>`` with TTL ``expires_in - 120`` seconds,
shared by every gunicorn worker so they do not each fetch a token (Daraja
throttles the token endpoint). Without Redis, an in-process dict is used:
fine for one worker and for tests.
"""
import logging
import threading
import time

from config import Config

logger = logging.getLogger(__name__)

_memory = {}
_lock = threading.Lock()
_redis = None
_redis_checked = False
SAFETY_MARGIN = 120


def _get_redis():
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    try:
        import redis  # type: ignore
        client = redis.Redis.from_url(Config.REDIS_URL, decode_responses=True,
                                      socket_connect_timeout=2)
        client.ping()
        _redis = client
    except Exception as e:
        logger.warning("Daraja token cache: Redis unavailable (%s); using memory", e)
        _redis = None
    return _redis


def _ttl(expires_in):
    """Daraja sends expires_in as a string ("3599")."""
    try:
        seconds = int(str(expires_in).strip())
    except (TypeError, ValueError):
        seconds = 3599
    return max(seconds - SAFETY_MARGIN, 60)


def get_token(tenant_id, fetch):
    """Cached token for ``tenant_id``; ``fetch()`` returns (token, expires_in)."""
    key = f"daraja:token:{tenant_id}"
    store = _get_redis()
    if store is not None:
        try:
            cached = store.get(key)
            if cached:
                return cached
            token, expires_in = fetch()
            store.setex(key, _ttl(expires_in), token)
            return token
        except Exception as e:
            if not _is_fetch_error(e):
                logger.warning("Daraja token cache: Redis error (%s); using memory", e)
            else:
                raise
    with _lock:
        token, expires_at = _memory.get(key, (None, 0))
        if token and expires_at > time.time():
            return token
        token, expires_in = fetch()
        _memory[key] = (token, time.time() + _ttl(expires_in))
        return token


def forget(tenant_id):
    """Drop a cached token (credentials changed, or Daraja rejected it)."""
    key = f"daraja:token:{tenant_id}"
    _memory.pop(key, None)
    store = _get_redis()
    if store is not None:
        try:
            store.delete(key)
        except Exception:
            pass


def _is_fetch_error(error):
    from services.daraja.client import DarajaError
    return isinstance(error, DarajaError)
