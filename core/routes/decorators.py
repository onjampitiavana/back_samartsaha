
from functools import wraps
from typing import Any, Callable

from flask import flash, g, redirect, url_for

from core.services import auth_service, session_service
from core.services.session_service import SessionStatus

LOGIN_ENDPOINT = "auth.login"
FORBIDDEN_ENDPOINT = "main.dashboard"


def login_required(view: Callable) -> Callable:
    """Let the view run only for a member whose Supabase session is valid.

    A session Supabase rejected is cleared ("expired"); if Supabase was
    merely unreachable, the tokens are kept so a network hiccup doesn't log
    the member out."""
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        """Run the view only if the access check passes."""
        if not session_service.has_session():
            flash("Connectez-vous pour continuer.", "error")
            return redirect(url_for(LOGIN_ENDPOINT))

        status = session_service.restore_session()
        if status is SessionStatus.UNAVAILABLE:
            flash("Le service de connexion est momentanément injoignable. Réessayez dans un "
                  "instant.", "error")
            return redirect(url_for(LOGIN_ENDPOINT))
        if status is not SessionStatus.OK:
            session_service.end_session()  # before flash(): flashes live in the session
            flash("Votre session a expiré. Veuillez vous reconnecter.", "error")
            return redirect(url_for(LOGIN_ENDPOINT))

        g.uid = g.auth_user.id
        return view(*args, **kwargs)
    return wrapped


def role_required(*allowed_roles: str) -> Callable:
    """Let the view run only for signed-in members whose role is one of
    `allowed_roles`. Fails closed: if the role can't be read (no service
    key, database error), access is refused."""
    def decorator(view: Callable) -> Callable:
        """Wrap `view` with the role check."""
        @wraps(view)
        @login_required
        def wrapped(*args: Any, **kwargs: Any):
            """Run the view only if the access check passes."""
            role = auth_service.lookup_role(g.uid)
            session_service.set_display_role(role)
            if role not in allowed_roles:
                flash("Vous n'avez pas accès à cette page.", "error")
                return redirect(url_for(FORBIDDEN_ENDPOINT))
            g.current_role = role
            return view(*args, **kwargs)
        return wrapped
    return decorator
