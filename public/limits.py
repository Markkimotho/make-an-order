"""Rate limits for the public (no-login) pages.

Storage is RATELIMIT_STORAGE_URI: Redis in production (shared by every
gunicorn worker), memory:// in tests or when no Redis is configured. If
Redis goes down, limits fall back to per-process memory and errors are
swallowed: weaker limiting, but a customer's pay link keeps working.

The per-invoice prompt cap is not here: it is counted from payment_intents
under the invoice lock in public/routes.py, which concurrent requests cannot
race past.
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(get_remote_address, swallow_errors=True, in_memory_fallback_enabled=True,
                  headers_enabled=True)

