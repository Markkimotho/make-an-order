"""Public, no-login pages under /o/<slug>: the business comes from the slug,
the record from a random stored token. See public/routes.py."""
from public.limits import limiter  # noqa: F401
from public.routes import public_bp  # noqa: F401
import public.refill  # noqa: F401,E402  registers the refill routes
import public.duka  # noqa: F401,E402  registers the duka order routes
import public.campaign  # noqa: F401,E402  registers the campaign routes
