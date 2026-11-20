"""Admin (and SuperAdmin) use cases: their managers and the staff of their
cooperatives - agents, auditors and consultants."""

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from core.models import ROLE_LABELS_FR, STAFF_ROLES, CooperativeRepository, Roles, role_label
from core.models.base import Row
from core.models.repositories import ProfileRepository, RoleRepository
from core.services import account_service, auth_service, email_check, mail_service
from core.services.account_service import AccountServiceError, NewAccount
from core.services.clients import service_db
from core.services.password_service import generate_password

# Shown to the administrator exactly as written (product requirement).
EMAIL_NOT_FOUND = "Adresse email introuvable"


CREDENTIALS_SUBJECT = "Vos accès Smart Saha"
CREDENTIALS_BODY = (
    "Bonjour, vous êtes inscrit pour {role} dans la coopérative {cooperative}. "
    "Veuillez vous connecter avec votre email et mot de passe. \n"
    "Mail : {email} \n"
    "Mot de passe : {password}"
)
NO_COOPERATIVE_YET = "(aucune coopérative attribuée pour le moment)"

log = logging.getLogger(__name__)


@dataclass
class PersonnelOverview:
    """The cooperatives an admin staffs and the members attached to them."""
    cooperatives: List[Row]                       # cooperatives the actor staffs
    members: List[Row] = field(default_factory=list)
    managers: List[Row] = field(default_factory=list)   # admins: their managers


# The administrator's staff page, kept a few seconds per admin: opening it
# again (or the creation page right after) costs no database round trip.
# Every write that changes it calls invalidate_overviews().
_OVERVIEW_TTL_S = 20
_overviews: Dict[str, Tuple[float, PersonnelOverview]] = {}
_overviews_lock = threading.Lock()
# public.admin_personnel_overview (migrations/20261029_admin_cooperatives.sql):
# None until tried, False when the migration has not been run yet.
_rpc_available: Optional[bool] = None


def invalidate_overviews(admin_uid: Optional[str] = None) -> None:
    """Forget the cached staff page of `admin_uid` (every admin if None)."""
    with _overviews_lock:
        if admin_uid is None:
            _overviews.clear()
        else:
            _overviews.pop(admin_uid, None)


def get_overview(actor_uid: str, with_members: bool = True,
                 role: Optional[str] = None) -> PersonnelOverview:
    """The actor's cooperatives (his own and his managers'), his managers and
    every member attached to them. `role`: the actor's role when the caller
    already knows it (saves reading his profile)."""
    if role == Roles.ADMIN:
        overview = _admin_overview(actor_uid)
        return copy.deepcopy(overview if with_members else
                             PersonnelOverview(overview.cooperatives, [], overview.managers))

    actor = account_service.load_actor(actor_uid)
    try:
        managers = account_service.admin_managers(actor)
        cooperatives = account_service.manageable_cooperatives(actor, managers)
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Could not load the cooperatives of %s", actor_uid)
        raise AccountServiceError("Impossible de charger votre personnel. Veuillez réessayer.") from exc
    members = account_service.list_team(actor, cooperatives) if with_members else []
    _attach_status(members)
    return PersonnelOverview(
        cooperatives=cooperatives, members=members,
        managers=[{"uuid": m["uuid"], "name": account_service.manager_label(m)}
                  for m in managers])


def _admin_overview(admin_uid: str) -> PersonnelOverview:
    """get_overview() for an administrator: cached, one round trip."""
    with _overviews_lock:
        hit = _overviews.get(admin_uid)
    if hit and time.monotonic() - hit[0] < _OVERVIEW_TTL_S:
        return hit[1]
    try:
        managers, cooperatives, members = _load_admin_rows(admin_uid)
        role_names = RoleRepository(service_db()).id_to_name()
    except Exception as exc:
        log.exception("Could not load the staff of admin %s", admin_uid)
        raise AccountServiceError("Impossible de charger votre personnel. Veuillez réessayer.") from exc

    # Same decoration as account_service.manageable_cooperatives / list_team.
    names = {m["uuid"]: account_service.manager_label(m) for m in managers}
    for c in cooperatives:
        c["manager_id"] = c.get("owner") if c.get("owner") in names else None
        c["manager_name"] = names.get(c.get("owner"))
    coops = {c["uuid"]: c for c in cooperatives}
    for m in members:
        coop = coops.get(m.get("cooperative_id")) or {}
        m["role"] = role_names.get(m.get("role_id"))
        m["cooperative_name"] = coop.get("name")
        m["manager_name"] = coop.get("manager_name")
        if m["role"] == Roles.MANAGER:
            m["cooperative_count"] = sum(1 for c in cooperatives if c.get("owner") == m["uuid"])

    _attach_status(members)
    overview = PersonnelOverview(
        cooperatives=cooperatives, members=members,
        managers=[{"uuid": m["uuid"], "name": names[m["uuid"]]} for m in managers])
    with _overviews_lock:
        _overviews[admin_uid] = (time.monotonic(), overview)
    return overview


def _load_admin_rows(admin_uid: str) -> Tuple[List[Row], List[Row], List[Row]]:
    """(managers, cooperatives, members) of an administrator: one SQL call
    when the migration is installed, otherwise three queries."""
    global _rpc_available
    db = service_db()
    if _rpc_available is not False:
        try:
            data = db.rpc("admin_personnel_overview", {"p_admin": admin_uid}).execute().data
            _rpc_available = True
            return (list(data.get("managers") or []), list(data.get("cooperatives") or []),
                    list(data.get("members") or []))
        except Exception as exc:
            if "PGRST202" in str(exc) or "admin_personnel_overview" in str(exc):
                _rpc_available = False
                log.warning("admin_personnel_overview() is missing: run "
                            "migrations/20261029_admin_cooperatives.sql in Supabase. "
                            "Falling back to three queries.")
            else:
                log.warning("admin_personnel_overview() failed (%s): falling back "
                            "to three queries.", exc)

    profiles = ProfileRepository(db)
    linked = profiles.list_by_admin(admin_uid)            # managers + staff he created
    manager_role = RoleRepository(db).get_id(Roles.MANAGER)
    managers = [p for p in linked if p.get("role_id") == manager_role]
    cooperatives = CooperativeRepository(db).list_owned_by_any(
        [admin_uid, *(m["uuid"] for m in managers)])
    seen = {p["uuid"] for p in linked}
    members = linked + [p for p in profiles.list_by_cooperatives([c["uuid"] for c in cooperatives])
                        if p["uuid"] not in seen]
    members.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return managers, cooperatives, members


def create_staff_member(actor_uid: str, account: NewAccount, role_name: str,
                        cooperative_id: Optional[str]) -> Row:
    """Create a confirmed manager (no cooperative), or agent / auditor /
    consultant attached to one of the actor's cooperatives, and e-mail them
    their credentials.

      1. the address must be able to receive mail, else EMAIL_NOT_FOUND;
      2. the password is generated (8 characters, see password_service)
         unless the caller chose one;
      3. the account is created, then the credentials are e-mailed. If the
         e-mail can't be delivered, the account is deleted again: nobody
         could ever learn its password."""
    if role_name not in STAFF_ROLES:
        raise AccountServiceError("Choisissez un rôle parmi : " + ", ".join(role_label(r) for r in STAFF_ROLES) + ".")
    ensure_deliverable(account.email)

    actor = account_service.load_actor(actor_uid)
    account.password = account.password or generate_password()
    profile = account_service.create_member(actor, account, role_name, cooperative_id)
    invalidate_overviews(actor_uid)

    try:
        coop = (CooperativeRepository(service_db()).get(profile["cooperative_id"])
                if profile.get("cooperative_id") else None)
    except Exception:
        log.exception("Could not read the cooperative of new member %s", profile.get("uuid"))
        coop = None
    body = CREDENTIALS_BODY.format(
        role=ROLE_LABELS_FR.get(role_name, role_name),
        cooperative=(coop or {}).get("name") or NO_COOPERATIVE_YET,
        email=profile.get("email") or account.email,
        password=account.password,
    )
    try:
        send_credentials_or_rollback(profile, account.email, CREDENTIALS_SUBJECT, body)
    finally:
        invalidate_overviews(actor_uid)
    return profile


def ensure_deliverable(email: str) -> None:
    """Refuse to create an account whose credentials can't be e-mailed:
    mail must be configured and the address able to receive it."""
    if not mail_service.is_configured():
        raise AccountServiceError("L'envoi d'e-mails n'est pas configuré sur le serveur : le "
                                  "compte n'a pas été créé.")
    if not email_check.can_receive_mail(email):
        raise AccountServiceError(EMAIL_NOT_FOUND)


def send_credentials_or_rollback(profile: Row, email: str, subject: str, body: str) -> None:
    """E-mail the credentials of the account just created. If they can't be
    delivered, the account is deleted again: nobody could ever learn its
    password."""
    try:
        mail_service.send(profile.get("email") or email, subject, body)
    except mail_service.RecipientRefused as exc:
        account_service.delete_account(profile["uuid"])
        raise AccountServiceError(EMAIL_NOT_FOUND) from exc
    except mail_service.MailError as exc:
        account_service.delete_account(profile["uuid"])
        raise AccountServiceError("L'e-mail contenant les identifiants n'a pas pu être "
                                  f"envoyé ({exc}) Le compte n'a donc pas été créé.") from exc


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------
NOT_IN_TEAM = "Ce membre est introuvable ou ne fait pas partie de votre personnel."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n > 1 else ''}"


def _blocking_data(db, uid: str) -> Optional[str]:
    """Why deleting `uid` would destroy or orphan data, or None.

    Deleting the auth user cascades to every row he owns (cooperative,
    farmers, parcels, audits_bio: owner ON DELETE CASCADE) and is refused
    by Postgres for forms he created or published (ON DELETE RESTRICT).
    Those cases are refused here with a message the admin can act on."""
    coops = CooperativeRepository(db).count_where("owner", uid)
    if coops:
        return (f"Ce gestionnaire est encore responsable de {_plural(coops, 'coopérative')} : "
                "confiez-les d'abord à un autre gestionnaire depuis la page Coopératives.")
    for table, column, what in (
            ("farmers", "owner", "producteur"),
            ("parcels", "owner", "parcelle"),
            ("audits_bio", "owner", "audit"),
            ("formulaires", "created_by", "formulaire d'enquête")):
        n = _count(db, table, column, uid)
        if n:
            return (f"Ce membre ne peut pas être supprimé : {_plural(n, what)} lui "
                    f"{'sont' if n > 1 else 'est'} encore rattaché{'s' if n > 1 else ''}.")
    return None


def _count(db, table: str, column: str, value: str) -> int:
    response = (db.table(table).select(column, count="exact", head=True)
                .eq(column, value).execute())
    return int(getattr(response, "count", None) or 0)


def delete_staff_member(actor_uid: str, member_uid: str,
                        role: Optional[str] = None) -> Row:
    """Delete a member of the actor's staff (manager, agent, auditor,
    consultant): his profile, then his auth account. Returns the deleted
    profile (as listed). Raises AccountServiceError.

    The member must appear on the actor's own staff page (same scope as the
    listing), never be the actor himself nor an administrator, and own no
    data that the deletion would destroy (see _blocking_data). If Supabase
    refuses to delete the auth user (survey answers he submitted reference
    it), the profile is put back: nothing changes."""
    if member_uid == actor_uid:
        raise AccountServiceError("Vous ne pouvez pas supprimer votre propre compte.")
    overview = get_overview(actor_uid, role=role)
    member = next((m for m in overview.members if m.get("uuid") == member_uid), None)
    if member is None:
        log.warning("%s tried to delete %s, who is not on their staff", actor_uid, member_uid)
        raise AccountServiceError(NOT_IN_TEAM)
    if member.get("role") not in STAFF_ROLES:
        raise AccountServiceError("Seuls les gestionnaires, agents, auditeurs et consultants "
                                  "peuvent être supprimés ici.")

    db = service_db()
    profiles = ProfileRepository(db)
    try:
        blocking = _blocking_data(db, member_uid)
        if blocking:
            raise AccountServiceError(blocking)
        stored = profiles.get_by_uuid(member_uid)
        if stored is None:
            raise AccountServiceError(NOT_IN_TEAM)
        profiles.delete_by("uuid", member_uid)
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Could not delete the profile of %s", member_uid)
        raise AccountServiceError("La suppression a échoué. Veuillez réessayer.") from exc

    try:
        db.auth.admin.delete_user(member_uid)
    except Exception as exc:
        log.warning("delete_user(%s) refused: %s - restoring the profile", member_uid, exc)
        try:
            # `id` is generated by the database: let it assign a new one.
            profiles.insert({k: v for k, v in stored.items() if k != "id"})
        except Exception:
            log.exception("Could not restore the profile of %s - restore it manually: %s",
                          member_uid, stored)
        raise AccountServiceError("Ce membre ne peut pas être supprimé : des données (réponses "
                                  "d'enquête, formulaires publiés…) sont encore rattachées à son "
                                  "compte.") from exc

    auth_service.forget_role(member_uid)
    invalidate_overviews()                           # his admin's page, and the actor's
    log.info("%s %s deleted by %s", member.get("role"), member_uid, actor_uid)
    return member


# --------------------------------------------------------------------------
# Status (active / inactive), edition, password reset
# (migrations/20261126_staff_is_active.sql)
# --------------------------------------------------------------------------
STATUS_MISSING = ("Le statut des comptes n'est pas encore installé : exécutez "
                  "migrations/20261126_staff_is_active.sql dans Supabase.")
# Supabase Auth ban while deactivated (~100 years); "none" lifts it.
_BAN_FOREVER = "876000h"


def _attach_status(members: List[Row]) -> None:
    """Make sure every member has `is_active` (read separately when the
    listing came without it; True when the column does not exist yet)."""
    missing = [m["uuid"] for m in members if "is_active" not in m]
    if not missing:
        return
    found: Dict[str, bool] = {}
    try:
        rows = (service_db().table("user_profiles").select("uuid, is_active")
                .in_("uuid", missing).execute().data) or []
        found = {r["uuid"]: r.get("is_active") is not False for r in rows}
    except Exception:
        pass                                         # column not installed yet
    for m in members:
        m.setdefault("is_active", found.get(m["uuid"], True))


def _team_member(actor_uid: str, member_uid: str, role: Optional[str]) -> Row:
    """The member as listed on the actor's staff page, or AccountServiceError."""
    if member_uid == actor_uid:
        raise AccountServiceError("Cette action n'est pas possible sur votre propre compte.")
    overview = get_overview(actor_uid, role=role)
    member = next((m for m in overview.members if m.get("uuid") == member_uid), None)
    if member is None:
        log.warning("%s tried to act on %s, who is not on their staff", actor_uid, member_uid)
        raise AccountServiceError(NOT_IN_TEAM)
    if member.get("role") not in STAFF_ROLES:
        raise AccountServiceError("Seuls les gestionnaires, agents, auditeurs et consultants "
                                  "peuvent être gérés ici.")
    return member


def set_members_active(actor_uid: str, member_uids: List[str], active: bool,
                       role: Optional[str] = None) -> List[Row]:
    """Activate / deactivate members of the actor's staff: user_profiles
    .is_active, plus a Supabase Auth ban (no sign-in, no session refresh)
    while inactive. Returns the members changed."""
    if not member_uids:
        raise AccountServiceError("Sélectionnez au moins un membre.")
    members = [_team_member(actor_uid, uid, role) for uid in dict.fromkeys(member_uids)]
    db = service_db()
    try:
        db.table("user_profiles").update({"is_active": active}) \
            .in_("uuid", [m["uuid"] for m in members]).execute()
    except Exception as exc:
        if "is_active" in str(exc):
            raise AccountServiceError(STATUS_MISSING) from exc
        log.exception("Could not change the status of %s", member_uids)
        raise AccountServiceError("Le statut n'a pas pu être modifié. Veuillez réessayer.") from exc
    failed = []
    for m in members:
        try:
            db.auth.admin.update_user_by_id(
                m["uuid"], {"ban_duration": "none" if active else _BAN_FOREVER})
        except Exception:
            log.exception("Could not %s the auth user %s", "unban" if active else "ban", m["uuid"])
            failed.append(m)
        auth_service.forget_role(m["uuid"])
    invalidate_overviews()
    if failed:
        names = ", ".join(m.get("full_name") or m.get("email") for m in failed)
        raise AccountServiceError(f"Statut enregistré, mais la connexion n'a pas pu être "
                                  f"{'rétablie' if active else 'bloquée'} pour : {names}. Réessayez.")
    for m in members:
        m["is_active"] = active
    return members


def update_staff_member(actor_uid: str, member_uid: str, values: Row,
                        role: Optional[str] = None) -> Row:
    """Edit name, username, mobile and (agents, auditors, consultants) the
    cooperative of a member of the actor's staff. `values` is validated."""
    from core.forms.validators import (MOBILE_MESSAGE, MOBILE_RE, USERNAME_RE, matches,
                                       person_name_problem)
    member = _team_member(actor_uid, member_uid, role)
    changes: Row = {}
    errors: List[str] = []

    full_name = (values.get("full_name") or "").strip()
    if "full_name" in values and full_name != (member.get("full_name") or ""):
        problem = person_name_problem(full_name)
        if problem:
            errors.append(problem)
        changes["full_name"] = full_name

    username = (values.get("username") or "").strip()
    if "username" in values and username != member.get("username"):
        if not matches(USERNAME_RE, username):
            errors.append("Le nom d'utilisateur : 3 à 30 caractères (lettres, chiffres, « _ » et « . »).")
        changes["username"] = username

    mobile = (values.get("mobile") or "").strip() or None
    if "mobile" in values and mobile != (member.get("mobile") or None):
        if mobile and not matches(MOBILE_RE, mobile):
            errors.append(MOBILE_MESSAGE)
        changes["mobile"] = mobile

    coop = values.get("cooperative_id")
    if coop is not None and member.get("role") != Roles.MANAGER and coop != member.get("cooperative_id"):
        overview = get_overview(actor_uid, with_members=False, role=role)
        if coop not in {c["uuid"] for c in overview.cooperatives}:
            errors.append("Choisissez l'une de vos coopératives.")
        else:
            from core.services import cooperative_service
            from core.services.errors import ServiceError
            try:
                cooperative_service.ensure_active(coop, "un membre du personnel")
            except ServiceError as exc:
                errors.append(str(exc))
        changes["cooperative_id"] = coop

    if errors:
        raise AccountServiceError(" ".join(errors))
    if not changes:
        return member
    db = service_db()
    profiles = ProfileRepository(db)
    try:
        if "username" in changes and profiles.username_taken(changes["username"]):
            raise AccountServiceError("Ce nom d'utilisateur est déjà pris.")
        db.table("user_profiles").update(changes).eq("uuid", member_uid).execute()
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Could not update the profile of %s", member_uid)
        raise AccountServiceError("La modification a échoué. Veuillez réessayer.") from exc
    invalidate_overviews()
    member.update(changes)
    return member


RESET_SUBJECT = "Votre nouveau mot de passe Smart Saha"
RESET_BODY = (
    "Bonjour, votre administrateur a réinitialisé votre mot de passe Smart Saha. \n"
    "Mail : {email} \n"
    "Nouveau mot de passe : {password} \n"
    "Pensez à le changer depuis Paramètres après votre connexion."
)


def reset_member_password(actor_uid: str, member_uid: str, role: Optional[str] = None) -> Row:
    """Generate a new password for a member of the actor's staff and e-mail
    it. The e-mail is checked first; if it fails after the change, the
    administrator is told to try again (the old password no longer works)."""
    member = _team_member(actor_uid, member_uid, role)
    if not mail_service.is_configured():
        raise AccountServiceError("L'envoi d'e-mails n'est pas configuré sur le serveur : le mot "
                                  "de passe n'a pas été réinitialisé.")
    password = generate_password()
    try:
        service_db().auth.admin.update_user_by_id(member_uid, {"password": password})
    except Exception as exc:
        log.exception("Could not reset the password of %s", member_uid)
        raise AccountServiceError("Le mot de passe n'a pas pu être réinitialisé. "
                                  "Veuillez réessayer.") from exc
    try:
        mail_service.send(member["email"], RESET_SUBJECT,
                          RESET_BODY.format(email=member["email"], password=password))
    except mail_service.MailError as exc:
        raise AccountServiceError(f"Le mot de passe a été changé mais l'e-mail n'a pas pu être "
                                  f"envoyé ({exc}). Réinitialisez-le à nouveau.") from exc
    log.info("Password of %s reset by %s", member_uid, actor_uid)
    return member
