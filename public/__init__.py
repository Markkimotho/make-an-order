"""Public, no-login pages under /o/<slug>: the business comes from the slug,
the record from a random stored token. See public/routes.py."""
from public.limits import limiter  # noqa: F401
from public.routes import public_bp  # noqa: F401
