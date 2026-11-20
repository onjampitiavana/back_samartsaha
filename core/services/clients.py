"""
Which Supabase client a service uses.

  user_db()     the signed-in member's client, restored by the session
                service (Row Level Security applies). Only valid after
                login_required / role_required ran for the request.
  service_db()  the administrator client (bypasses RLS), shared for the
                rest of the request. Raises ServiceError if no service key
                is configured.
"""

from typing import Any

from flask import g, has_request_context

from core import config
from core.services.errors import ServiceError


def user_db() -> Any:
    """The current member's Supabase client."""
    client = g.get("db")
    if client is None:
        raise RuntimeError("user_db() called outside an authenticated request "
                           "(missing @login_required?)")
    return client


def service_db() -> Any:
    """The administrator client, created once per request."""
    if has_request_context() and g.get("service_db") is not None:
        return g.service_db
    client = config.get_service_client()
    if client is None:
        raise ServiceError("Cette action est momentanément indisponible : la configuration du "
                           "serveur est incomplète.")
    if has_request_context():
        g.service_db = client
    return client


def optional_service_db() -> Any:
    """Like service_db(), but None instead of an error without a key."""
    try:
        return service_db()
    except ServiceError:
        return None
