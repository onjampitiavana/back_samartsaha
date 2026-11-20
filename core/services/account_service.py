"""
Account creation and team listing, for every role that creates accounts.

One policy for the whole hierarchy (see core/models/roles.py):

  actor        may create                        in cooperatives
  superadmin   admin, manager and staff          any (admins: none)
  admin        manager, agent, auditor, consult. his own and his managers'
  manager      agent                             his own (cooperative.owner)

  admin --(1..n)--> manager      user_profiles.admin_id (one admin per manager)
  manager --(1..n)--> cooperative cooperative.owner
  staff -> one cooperative       user_profiles.cooperative_id, + admin_id

Managers join no cooperative: their admin assigns cooperatives to them (or
they register their own). Everyone else joins exactly one cooperative.

The actor's role and cooperative are always read from the database here,
never taken from the request, so a tampered form can't widen them.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from gotrue.errors import AuthError

from core.models import (
    CooperativeRepository, ProfileRepository, RoleRepository, Roles,
    can_create, role_label,
)
from core.models.base import Row
from core.services.clients import service_db
from core.services.errors import ServiceError, friendly_auth_error

log = logging.getLogger(__name__)


class AccountServiceError(ServiceError):
    """Account creation / team listing failure with a user-facing message."""


@dataclass
class NewAccount:
    """What the creator provides for a new account."""
    email: str
    password: str
    full_name: str
    username: Optional[str] = None     # generated from the e-mail if None
    mobile: Optional[str] = None


@dataclass
class Actor:
    """A member acting on accounts, as stored in the database."""
    uid: str
    role: Optional[str]
    profile: Optional[Row]

    @property
    def cooperative_id(self) -> Optional[str]:
        """The cooperative in the actor's own profile (legacy managers), if any."""
        return (self.profile or {}).get("cooperative_id")

    @property
    def admin_id(self) -> Optional[str]:
        """The actor's referent administrator, if any."""
        return (self.profile or {}).get("admin_id")


# --------------------------------------------------------------------------
# Actor & scope
# --------------------------------------------------------------------------
def load_actor(uid: str) -> Actor:
    """The actor's profile and role, read with the service client."""
    db = service_db()
    try:
        profile = ProfileRepository(db).get_by_uuid(uid)
        role = RoleRepository(db).get_name(profile.get("role_id")) if profile else None
    except Exception as exc:
        log.exception("Could not load actor %s", uid)
        raise AccountServiceError("La base de données est momentanément injoignable. Veuillez "
                                  "réessayer.") from exc
    if profile is None:
        raise AccountServiceError("Votre profil est introuvable. Contactez votre "
                                  "administrateur.")
    return Actor(uid=uid, role=role, profile=profile)


def admin_managers(actor: Actor) -> List[Row]:
    """The managers whose referent administrator is `actor`."""
    if actor.role != Roles.ADMIN:
        return []
    db = service_db()
    role_id = RoleRepository(db).get_id(Roles.MANAGER)
    if role_id is None:
        raise AccountServiceError("Le rôle « Gestionnaire » est absent de la base de données. "
                                  "Contactez l'administrateur technique.")
    return ProfileRepository(db).list_by_admin(actor.uid, role_id=role_id)


def manager_label(profile: Row) -> str:
    """How a manager is named in lists: exactly his full_name as stored
    (username or e-mail only for a profile without one)."""
    return ((profile.get("full_name") or "").strip()
            or profile.get("username") or profile.get("email") or "—")


def manageable_cooperatives(actor: Actor,
                            managers: Optional[List[Row]] = None) -> List[Row]:
    """Cooperatives (uuid, name, owner) whose team `actor` may see and grow.
    For an admin: his own and those of his managers, each with
    `manager_id` / `manager_name` when a manager owns it."""
    coops = CooperativeRepository(service_db())
    if actor.role == Roles.SUPERADMIN:
        return coops.list_names()
    if actor.role == Roles.ADMIN:
        managers = admin_managers(actor) if managers is None else managers
        names = {m["uuid"]: manager_label(m) for m in managers}
        rows = coops.list_owned_by_any([actor.uid, *names])
        for c in rows:
            c["manager_id"] = c.get("owner") if c.get("owner") in names else None
            c["manager_name"] = names.get(c.get("owner"))
        return rows
    if actor.role == Roles.MANAGER:
        rows = coops.list_owned_by(actor.uid)
        legacy = actor.cooperative_id
        if legacy and legacy not in {c["uuid"] for c in rows}:
            coop = coops.get(legacy)
            rows.append(coop or {"uuid": legacy, "name": None})
        return rows
    return []


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------
def list_team(actor: Actor, cooperatives: List[Row],
              role_name: Optional[str] = None) -> List[Row]:
    """Members attached to `cooperatives` (optionally of one role), each
    decorated with `role` and `cooperative_name`."""
    db = service_db()
    try:
        role_names = RoleRepository(db).id_to_name()
        role_id = None
        if role_name is not None:
            role_id = next((i for i, n in role_names.items() if n == role_name), None)
            if role_id is None:
                raise AccountServiceError(f"Le rôle « {role_label(role_name)} » est absent de "
                                          "la base de données. Contactez l'administrateur "
                                          "technique.")
        members = ProfileRepository(db).list_by_cooperatives(
            [c["uuid"] for c in cooperatives], role_id=role_id)
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Could not list the team of %s", actor.uid)
        raise AccountServiceError("Impossible de charger votre équipe. Veuillez réessayer.") from exc

    if actor.role == Roles.ADMIN:
        # Managers have no cooperative: they are found through admin_id.
        try:
            seen = {m["uuid"] for m in members}
            members += [m for m in ProfileRepository(db).list_by_admin(actor.uid, role_id=role_id)
                        if m["uuid"] not in seen]
        except Exception as exc:
            log.exception("Could not list the members of admin %s", actor.uid)
            raise AccountServiceError("Impossible de charger votre équipe. Veuillez réessayer.") from exc
        members.sort(key=lambda m: m.get("created_at") or "", reverse=True)

    coop_names = {c["uuid"]: c.get("name") for c in cooperatives}
    coop_managers = {c["uuid"]: c.get("manager_name") for c in cooperatives}
    for m in members:
        m["role"] = role_names.get(m.get("role_id"))
        m["cooperative_name"] = coop_names.get(m.get("cooperative_id"))
        m["manager_name"] = coop_managers.get(m.get("cooperative_id"))
        if m["role"] == Roles.MANAGER:
            m["cooperative_count"] = sum(1 for c in cooperatives
                                         if c.get("owner") == m["uuid"])
    return members


def list_by_role(role_name: str) -> List[Row]:
    """Every profile with `role_name` (SuperAdmin: administrators)."""
    db = service_db()
    try:
        role_id = RoleRepository(db).get_id(role_name)
        if role_id is None:
            raise AccountServiceError(f"Le rôle « {role_label(role_name)} » est absent de la "
                                      "base de données. Contactez l'administrateur technique.")
        return ProfileRepository(db).list_by_role(role_id)
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Could not list %s profiles", role_name)
        raise AccountServiceError("Impossible de charger la liste. Veuillez réessayer.") from exc


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------
def create_member(actor: Actor, account: NewAccount, role_name: str,
                  cooperative_id: Optional[str] = None) -> Row:
    """Create a confirmed account with `role_name`, checked against the
    hierarchy and the actor's cooperatives.

    Managers join no cooperative and get the creating admin as referent.
    Every other staff member joins one of the actor's cooperatives (a
    manager's single cooperative is picked for him) and inherits the
    actor's admin."""
    if not can_create(actor.role, role_name):
        raise AccountServiceError("Vous ne pouvez pas créer de compte « "
                                  f"{role_label(role_name)} ».")

    extra: Dict[str, Any] = {}
    if role_name != Roles.ADMIN:
        if role_name == Roles.MANAGER:
            cooperative_id = None
        else:
            try:
                allowed = {c["uuid"] for c in manageable_cooperatives(actor)}
            except AccountServiceError:
                raise
            except Exception as exc:
                log.exception("Could not resolve the cooperatives of %s", actor.uid)
                raise AccountServiceError("La base de données est momentanément injoignable. "
                                          "Veuillez réessayer.") from exc
            if not cooperative_id and actor.role == Roles.MANAGER and len(allowed) == 1:
                cooperative_id = next(iter(allowed))
            if not cooperative_id:
                raise AccountServiceError("Choisissez la coopérative que rejoint ce membre.")
            if cooperative_id not in allowed:
                log.warning("%s (%s) tried to staff cooperative %s they don't manage",
                            actor.uid, actor.role, cooperative_id)
                raise AccountServiceError("Vous ne pouvez ajouter du personnel que dans vos "
                                          "propres coopératives.")
            # Late import: cooperative_service imports this module.
            from core.services import cooperative_service
            try:
                cooperative_service.ensure_active(cooperative_id, "un membre du personnel")
            except AccountServiceError:
                raise
            except ServiceError as exc:
                raise AccountServiceError(str(exc)) from exc
            extra["cooperative_id"] = cooperative_id

        if actor.role == Roles.ADMIN:
            extra["admin_id"] = actor.uid
        elif actor.role == Roles.MANAGER and actor.admin_id:
            extra["admin_id"] = actor.admin_id

    return create_confirmed_account(account, role_name, actor.uid, extra)


def delete_account(uid: str) -> None:
    """Remove an account created moments ago (profile, then auth user), when
    a later step of its creation failed. Best effort: logged if it fails."""
    db = service_db()
    try:
        ProfileRepository(db).query().delete().eq("uuid", uid).execute()
    except Exception:
        log.exception("Could not delete the profile %s - remove it manually.", uid)
    try:
        db.auth.admin.delete_user(uid)
    except Exception:
        log.exception("Could not delete the auth user %s - remove it manually.", uid)


def create_confirmed_account(account: NewAccount, role_name: str,
                             created_by: str,
                             extra_profile: Optional[Dict[str, Any]] = None) -> Row:
    """Create a confirmed Supabase Auth user and its user_profiles row.

    Low level: no hierarchy check (use create_member). If the profile insert
    fails after the auth user was created, that auth user is deleted again so
    no half-created account is left behind."""
    db = service_db()
    profiles = ProfileRepository(db)
    email = account.email.strip().lower()
    try:
        role_id = RoleRepository(db).get_id(role_name)
        if role_id is None:
            raise AccountServiceError(f"Le rôle « {role_label(role_name)} » est absent de la "
                                      "base de données. Contactez l'administrateur technique.")
        if profiles.get_by_email(email):
            raise AccountServiceError("Un compte existe déjà avec cette adresse e-mail.")
        if account.username is None:
            # Lower-cased like the stored e-mail, so 'Tojo@' and 'tojo@'
            # can't yield usernames differing only by case.
            username = profiles.available_username(email)
        elif profiles.username_taken(account.username):
            raise AccountServiceError("Ce nom d'utilisateur est déjà pris.")
        else:
            username = account.username
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Pre-checks for new %s %s failed", role_name, email)
        raise AccountServiceError("La base de données est momentanément injoignable. Veuillez "
                                  "réessayer.") from exc

    try:
        response = db.auth.admin.create_user({
            "email": email,
            "password": account.password,
            "email_confirm": True,
            "user_metadata": {"full_name": account.full_name,
                              "username": username,
                              "mobile": account.mobile},
        })
    except AuthError as exc:
        log.warning("create_user failed for new %s %s: %s", role_name, email, exc)
        raise AccountServiceError(friendly_auth_error(str(exc))) from exc
    except Exception as exc:
        log.exception("create_user failed for new %s %s", role_name, email)
        raise AccountServiceError("Le compte n'a pas pu être créé. Veuillez réessayer.") from exc

    user = response.user
    row: Row = {
        "uuid": user.id,
        "username": username,
        "full_name": account.full_name,
        "email": email,
        "mobile": account.mobile or None,
        "role_id": role_id,
        **(extra_profile or {}),
    }
    try:
        stored = profiles.insert(row)
    except Exception as exc:
        log.exception("Profile insert failed for new %s %s", role_name, user.id)
        try:
            db.auth.admin.delete_user(user.id)
        except Exception:
            log.exception("Could not remove the auth user %s after the failed "
                          "profile insert - remove it manually.", user.id)
        raise AccountServiceError("Le profil n'a pas pu être enregistré : le compte n'a pas "
                                  "été créé.") from exc

    log.info("%s %s created by %s", role_name.capitalize(), user.id, created_by)
    return stored or row
