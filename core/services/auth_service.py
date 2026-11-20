"""Sign in, sign out and public registration."""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from flask import session
from gotrue.errors import AuthRetryableError

from core import config
from core.forms.auth_forms import RegistrationInput
from core.models import ProfileRepository, Roles
from core.services import session_service
from core.services.clients import optional_service_db
from core.services.concurrency import Failed, parallel
from core.services.errors import ServiceError, friendly_auth_error

log = logging.getLogger(__name__)


class AuthServiceError(ServiceError):
    """Sign-in / registration failure with a user-facing message."""


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
# A role change reaches every request within this delay. Each lookup costs
# a round trip to Supabase, and every authenticated API call needs the role.
_ROLE_TTL_S = 60
_roles: Dict[str, Tuple[Optional[str], float]] = {}
_roles_lock = threading.Lock()


def lookup_role(uid: str) -> Optional[str]:
    """Role name read from the database (cached for a minute), or None if
    unknown / unreadable. Unreadable roles are never cached."""
    with _roles_lock:
        hit = _roles.get(uid)
    if hit and time.monotonic() - hit[1] < _ROLE_TTL_S:
        return hit[0]
    db = optional_service_db()
    if db is None:
        log.error("SUPABASE_SERVICE_ROLE_KEY is not set: roles can't be read.")
        return None
    try:
        role = ProfileRepository(db).get_role_name(uid)
    except Exception:
        log.exception("Could not read the role of %s", uid)
        return None
    with _roles_lock:
        _roles[uid] = (role, time.monotonic())
    return role


def forget_role(uid: str) -> None:
    """Drop the cached role of `uid` (after changing it)."""
    with _roles_lock:
        _roles.pop(uid, None)


def home_endpoint_for(role: Optional[str]) -> str:
    """Where a member lands after signing in."""
    if role == Roles.SUPERADMIN:
        return "superadmin.dashboard"
    return "main.dashboard"


# --------------------------------------------------------------------------
# Sign in / out
# --------------------------------------------------------------------------
def sign_in(email: str, password: str, remember: bool = False) -> Optional[str]:
    """Authenticate with Supabase and open the Flask session. Returns the
    member's role name (or None). Raises AuthServiceError on failure."""
    email = email.strip().lower()
    client = config.get_auth_client()        # pooled HTTPS connection
    # Supabase is ~0.5 s away: the role is read at the same time as the
    # password is checked, instead of after it (one round trip less).
    try:
        response, early = parallel(
            lambda: client.sign_in_with_password({"email": email, "password": password}),
            lambda: _role_by_email(email),
            return_errors=True)
        if isinstance(response, Failed):
            raise response.error
    except AuthRetryableError as exc:
        log.warning("Supabase auth unreachable during sign-in: %s", exc)
        raise AuthServiceError("Le service de connexion est momentanément injoignable. "
                               "Réessayez dans un instant.") from exc
    except Exception as exc:
        raise AuthServiceError(friendly_auth_error(str(exc))) from exc

    if not response.session or not response.user:
        raise AuthServiceError("La connexion a échoué. Vérifiez vos identifiants et réessayez.")

    forget_role(response.user.id)          # a fresh sign-in re-reads the role
    if isinstance(early, dict) and early.get("uuid") == response.user.id:
        role = early["role"]
        with _roles_lock:
            _roles[response.user.id] = (role, time.monotonic())
    else:                                   # e-mail not on the profile: by uuid
        role = lookup_role(response.user.id)
    session_service.open_session(response.session.access_token,
                                 response.session.refresh_token,
                                 response.user.id, role, remember)
    session_service.adopt_user(response.session.access_token, response.user)
    return role


def _role_by_email(email: str) -> Optional[dict]:
    """{uuid, role} of the profile with this e-mail, or None. Never raises:
    sign_in() then reads the role by uuid as before."""
    db = optional_service_db()
    if db is None:
        return None
    try:
        return ProfileRepository(db).role_by_email(email)
    except Exception as exc:
        log.info("Early role lookup failed (read again by uuid): %s", exc)
        return None


def sign_out() -> None:
    """Revoke the Supabase session (best effort) and clear the cookie."""
    if session_service.has_session():
        client = config.get_anon_client()
        try:
            client.auth.set_session(session["access_token"], session["refresh_token"])
            client.auth.sign_out()
        except Exception as exc:
            log.info("Supabase sign-out failed (session cleared anyway): %s", exc)
    session_service.end_session()


# --------------------------------------------------------------------------
# Public registration (/signup)
# --------------------------------------------------------------------------
@dataclass
class RegistrationResult:
    """Outcome of register(): signed in or not, plus non-fatal warnings."""
    signed_in: bool                       # Supabase returned a session
    first_name: str
    warnings: List[str] = field(default_factory=list)


def username_taken(username: str) -> bool:
    """True if another profile already uses this username. Needs the
    service key (RLS hides other members' rows from anon); without it, or
    on error, returns False and the unique constraint stays the guard."""
    db = optional_service_db()
    if db is None:
        return False
    try:
        return ProfileRepository(db).username_taken(username)
    except Exception as exc:
        log.warning("Username availability check failed: %s", exc)
        return False


def register(data: RegistrationInput) -> RegistrationResult:
    """Create the auth user, then its user_profiles row; sign the member in
    if Supabase returns a session (no e-mail confirmation required).

    The profile fields are also kept in user_metadata so a missing profile
    row can be rebuilt at sign-in (profile_service.recover_profile). A
    failed profile insert is reported as a warning: the auth account already
    exists, so failing the whole signup would only produce "already
    registered" on retry."""
    # Reject a taken username *before* creating the auth user.
    if username_taken(data.username):
        raise AuthServiceError("Ce nom d'utilisateur est déjà pris. Choisissez-en un autre.")

    client = config.get_anon_client()
    try:
        response = client.auth.sign_up({
            "email": data.email,
            "password": data.password,
            "options": {"data": {"full_name": data.full_name,
                                 "username": data.username,
                                 "mobile": data.mobile}},
        })
    except Exception as exc:
        raise AuthServiceError(friendly_auth_error(str(exc))) from exc

    user = response.user
    if not user:
        raise AuthServiceError("Votre compte n'a pas pu être créé. Veuillez réessayer.")

    result = RegistrationResult(signed_in=False,
                                first_name=data.full_name.split()[0])
    writer = optional_service_db() or client   # anon: RLS must allow insert
    try:
        ProfileRepository(writer).insert({
            "uuid": user.id,
            "username": data.username,
            "full_name": data.full_name,
            "email": data.email,
            "mobile": data.mobile,
        })
    except Exception as exc:
        log.error("Profile insert failed for new user %s: %s", user.id, exc)
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            result.warnings.append(
                "Votre compte a été créé, mais ce nom d'utilisateur vient d'être pris. "
                "Contactez le support pour en choisir un autre.")
        else:
            result.warnings.append(
                "Votre compte a été créé, mais votre profil n'a pas pu être enregistré. Il "
                "sera complété automatiquement à votre prochaine connexion.")

    if response.session:
        session_service.open_session(response.session.access_token,
                                     response.session.refresh_token,
                                     user.id, role=None)
        result.signed_in = True
    return result


# --------------------------------------------------------------------------
# Password change (Paramètres page, every role)
# --------------------------------------------------------------------------
def change_password(uid: str, email: Optional[str], current_password: str,
                    new_password: str) -> None:
    """Replace the signed-in member's password after checking the current
    one with Supabase. Only the password changes: the profile is never
    touched here. Raises AuthServiceError."""
    if not email:
        raise AuthServiceError("Votre adresse e-mail est introuvable : reconnectez-vous puis "
                               "réessayez.")
    checker = config.get_anon_client()
    try:
        response = checker.auth.sign_in_with_password({
            "email": email.strip().lower(), "password": current_password})
    except AuthRetryableError as exc:
        raise AuthServiceError("Le service de connexion est momentanément injoignable. "
                               "Réessayez dans un instant.") from exc
    except Exception as exc:
        log.info("Password change for %s: current password rejected (%s)", uid, exc)
        if "rate limit" in str(exc).lower() or "too many" in str(exc).lower():
            raise AuthServiceError(friendly_auth_error(str(exc))) from exc
        raise AuthServiceError("Le mot de passe actuel est incorrect.") from exc
    if not response.user or response.user.id != uid:
        raise AuthServiceError("Le mot de passe actuel est incorrect.")
    try:
        # The check opened a session of its own: close it, and only it.
        checker.auth.sign_out({"scope": "local"})
    except Exception:
        pass

    db = optional_service_db()
    if db is None:
        raise AuthServiceError("Cette action est momentanément indisponible : la configuration du "
                               "serveur est incomplète.")
    try:
        db.auth.admin.update_user_by_id(uid, {"password": new_password})
    except Exception as exc:
        log.warning("Password update failed for %s: %s", uid, exc)
        raise AuthServiceError(friendly_auth_error(str(exc))) from exc
    log.info("Password changed by %s", uid)
