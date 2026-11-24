"""Rate limits for the public (no-login) pages.

Storage is RATELIMIT_STORAGE_URI: Redis in production (shared by every
gunicorn worker), memory:// in tests or when no Redis is configured. If
Redis goes down, limits fall back to per-process memory and errors are
swallowed: weaker limiting, but a customer's pay link keeps working.
"""
from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(get_remote_address, swallow_errors=True, in_memory_fallback_enabled=True,
                  headers_enabled=True)


def per_invoice():
    """Key for limits shared by everyone pushing to one invoice."""
    return f"invoice:{(request.view_args or {}).get('slug')}:{(request.view_args or {}).get('pay_token')}"


def pushed(response):
    """Count only requests that actually sent an M-Pesa prompt."""
    return response.status_code == 201
