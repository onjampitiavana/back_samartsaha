import logging
from typing import Any, Optional
from flask import Flask
from gotrue.errors import AuthApiError
from core import config
from core.models import ProfileRepository, RoleRepository, Roles
from core.models.base import Row

_LIST_USERS_PAGE_SIZE = 200
_MIN_USERNAME_SEED = 3

def setup_superadmin(app: Flask) -> bool:
    """Run the bootstrap for `app`. True when the superadmin account is in
    place, False if skipped or failed."""
    log = app.logger
    email = (config.SUPER_ADMIN_MAIL or "").strip().lower()
    password = config.SUPER_ADMIN_PASSWORD or ""

    if not email or not password:
        log.info("SuperAdmin setup skipped: SUPER_ADMIN_MAIL / "
                 "SUPER_ADMIN_PASSWORD not set.")
        return False

    client = config.get_service_client()
    if client is None:
        log.error("SuperAdmin setup skipped: SUPABASE_SERVICE_ROLE_KEY is "
                  "required to create users and bypass Row Level Security.")
        return False

    try:
        return ensure_superadmin(client, email, password, log)
    except Exception:
        log.exception("SuperAdmin setup failed.")
        return False


def ensure_superadmin(client: Any, email: str, password: str,
                      log: logging.Logger) -> bool:
    """Core of setup_superadmin(), with its dependencies passed in."""
    profiles = ProfileRepository(client)
    role_id = RoleRepository(client).get_id(Roles.SUPERADMIN)
    if role_id is None:
        log.error("SuperAdmin setup skipped: no '%s' row in public.roles.",
                  Roles.SUPERADMIN)
        return False

    # -- existing profile: just promote it -------------------------------
    profile = profiles.get_by_email(email)
    if profile:
        _promote(profiles, profile, role_id, log)
        return True

    # -- no profile: get (or create) the auth user, then insert the row ----
    user = find_auth_user_by_email(client, email)
    if user is None:
        user = _create_auth_user(client, email, password, log)
    else:
        log.info("SuperAdmin auth user already exists without a profile; "
                 "reusing it (password left unchanged).")

    # A row may exist under this uuid with a different e-mail.
    profile = profiles.get_by_uuid(user.id)
    if profile:
        _promote(profiles, profile, role_id, log)
        return True

    meta = user.user_metadata or {}
    seed = meta.get("username") or email
    if len(seed.split("@")[0]) < _MIN_USERNAME_SEED:
        seed = Roles.SUPERADMIN
    profiles.insert({
        "uuid": user.id,
        "username": profiles.available_username(seed),
        "full_name": meta.get("full_name") or "Super Admin",
        "email": email,
        "role_id": role_id,
    })
    log.info("SuperAdmin profile created.")
    return True


def find_auth_user_by_email(client: Any, email: str) -> Optional[Any]:
    """Scan the Auth users (the Admin API has no lookup by e-mail)."""
    page = 1
    while True:
        users = client.auth.admin.list_users(page=page,
                                             per_page=_LIST_USERS_PAGE_SIZE)
        for user in users:
            if (user.email or "").lower() == email:
                return user
        if len(users) < _LIST_USERS_PAGE_SIZE:
            return None
        page += 1


def _promote(profiles: ProfileRepository, profile: Row, role_id: int,
             log: logging.Logger) -> None:
    if profile.get("role_id") == role_id:
        log.info("SuperAdmin already has the '%s' role.", Roles.SUPERADMIN)
        return
    profiles.set_role(profile["uuid"], role_id)
    log.info("SuperAdmin profile promoted to the '%s' role.", Roles.SUPERADMIN)


def _create_auth_user(client: Any, email: str, password: str,
                      log: logging.Logger) -> Any:
    try:
        response = client.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"full_name": "Super Admin"},
        })
    except AuthApiError:
        # Race with another process (e.g. the debug reloader) or an account
        # created meanwhile: reuse it if it is there now.
        user = find_auth_user_by_email(client, email)
        if user is None:
            raise
        return user
    log.info("SuperAdmin auth user created (e-mail confirmed).")
    return response.user
