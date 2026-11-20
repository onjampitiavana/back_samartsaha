"""Cooperatives and farmers registered by the signed-in member (Row Level
Security limits every query to the member's own rows).

Administrators are the exception for cooperatives: they see their own and
those of their managers (user_profiles.admin_id), read with the service key,
and register cooperatives on behalf of one of their managers."""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.forms.record_forms import CooperativeInput, FarmerInput
from core.models import CooperativeRepository, FarmerRepository, ParcelRepository, Roles
from core.models.base import BaseRepository, Row
from core.services import account_service, personnel_service
from core.services import page_rpc
from core.services.clients import service_db, user_db
from core.services.concurrency import parallel
from core.services.errors import ServiceError, friendly_db_error

log = logging.getLogger(__name__)

# Set to False once Supabase says cooperative.crops does not exist yet
# (migration not run): cooperatives are then written without it, with their
# first three crops in main_crop / second_crop / third_crop as before.
_crops_column = True
# Same for cooperative.region / commune (migrations/20261121_dashboard_map.sql).
_location_columns = True
LOCATION_COLUMNS = ("region", "commune")
# Same for cooperative.is_active (migrations/20261123_cooperative_is_active.sql).
_active_column = True


def _missing_column(exc: Exception, names) -> bool:
    text = str(exc)
    return any(n in text for n in names) and ("PGRST204" in text or "column" in text)


def _write(write, row: Row):
    """Run `write(row)`; without the crops (or region / commune) columns, run
    it again without them."""
    global _crops_column, _location_columns, _active_column
    if not _active_column:
        row = {k: v for k, v in row.items() if k != "is_active"}
    if not _crops_column:
        row = {k: v for k, v in row.items() if k != "crops"}
    if not _location_columns:
        row = {k: v for k, v in row.items() if k not in LOCATION_COLUMNS}
    try:
        return write(row)
    except Exception as exc:
        if "crops" in row and _missing_column(exc, ("crops",)):
            log.warning("cooperative.crops is missing: run "
                        "migrations/20261029_admin_cooperatives.sql in Supabase.")
            _crops_column = False
            return _write(write, row)
        if any(k in row for k in LOCATION_COLUMNS) and _missing_column(exc, LOCATION_COLUMNS):
            log.warning("cooperative.region / commune are missing: run "
                        "migrations/20261121_dashboard_map.sql in Supabase.")
            _location_columns = False
            return _write(write, row)
        if "is_active" in row and _missing_column(exc, ("is_active",)):
            _warn_missing_active()
            return _write(write, row)
        raise


def _warn_missing_active() -> None:
    global _active_column
    log.warning("cooperative.is_active is missing: run "
                "migrations/20261123_cooperative_is_active.sql in Supabase.")
    _active_column = False


# --------------------------------------------------------------------------
# Active / inactive
# --------------------------------------------------------------------------
INACTIVE_MESSAGE = ("La coopérative « {name} » est désactivée : impossible d'ajouter {what}. "
                    "Réactivez-la d'abord depuis la liste des coopératives.")


def ensure_active(cooperative_id: Optional[str], what: str) -> None:
    """Raise ServiceError when `cooperative_id` is a deactivated cooperative.
    `what`: what the user tried to add ("un producteur", "une parcelle"...).
    The database refuses it too (triggers of the migration); this check
    gives the user a clear message first."""
    if not cooperative_id or not _active_column:
        return
    try:
        row = BaseRepository.first(
            service_db().table("cooperative").select("name, is_active")
            .eq("uuid", cooperative_id).limit(1).execute())
    except Exception as exc:
        if _missing_column(exc, ("is_active",)):
            _warn_missing_active()
            return
        log.warning("Could not read the status of cooperative %s: %s", cooperative_id, exc)
        raise ServiceError("Impossible de vérifier le statut de la coopérative. "
                           "Veuillez réessayer.") from exc
    if row is not None and row.get("is_active") is False:
        raise ServiceError(INACTIVE_MESSAGE.format(name=row.get("name") or "", what=what))


def ensure_parcel_active(parcel_id: Optional[str], what: str) -> None:
    """ensure_active() for the cooperative of a parcel (audits)."""
    if not parcel_id or not _active_column:
        return
    try:
        parcel = BaseRepository.first(
            service_db().table("parcels").select("cooperative_id")
            .eq("uuid", parcel_id).limit(1).execute())
    except Exception as exc:
        log.warning("Could not read the cooperative of parcel %s: %s", parcel_id, exc)
        return                       # the insert itself reports a bad parcel
    ensure_active((parcel or {}).get("cooperative_id"), what)


def _scoped(uid: str, role: Optional[str], cooperative_uuid: str):
    """(client, row) for a cooperative the member may manage: a manager his
    own (Row Level Security), an admin his own and his managers'. Raises
    ServiceError when it is not one of them."""
    if role == Roles.ADMIN:
        actor = account_service.load_actor(uid)
        owners = [uid, *(m["uuid"] for m in account_service.admin_managers(actor))]
        db = service_db()
        rows = CooperativeRepository(db).rows(
            db.table("cooperative").select("*").eq("uuid", cooperative_uuid)
            .in_("owner", owners).limit(1).execute())
    else:
        db = user_db()
        rows = CooperativeRepository(db).rows(
            db.table("cooperative").select("*").eq("uuid", cooperative_uuid).limit(1).execute())
    if not rows:
        log.warning("%s (%s) tried to manage cooperative %s", uid, role, cooperative_uuid)
        raise ServiceError("Coopérative introuvable.")
    return db, rows[0]


def set_active(uid: str, role: Optional[str], cooperative_uuid: str, active: bool) -> Row:
    """Activate / deactivate a cooperative. Returns the stored row."""
    if not _active_column:
        raise ServiceError("Le statut des coopératives n'est pas encore disponible : la migration "
                           "20261123_cooperative_is_active.sql doit être appliquée.")
    try:
        db, current = _scoped(uid, role, cooperative_uuid)
        if "is_active" not in current:
            _warn_missing_active()
            return set_active(uid, role, cooperative_uuid, active)
        updated = CooperativeRepository(db).update(
            cooperative_uuid, {"is_active": active, "status": "active" if active else "not_active"})
    except ServiceError:
        raise
    except Exception as exc:
        log.warning("Cooperative status update failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc
    if not updated:
        raise ServiceError("Vous n'avez pas l'autorisation de modifier cette coopérative.")
    personnel_service.invalidate_overviews()
    return updated[0]


LINKED_LABELS = (
    ("farmers", "producteur", "producteurs"),
    ("parcels", "parcelle", "parcelles"),
    ("audits", "audit", "audits"),
    ("staff", "membre du personnel", "membres du personnel"),
    ("forms", "formulaire d'enquête", "formulaires d'enquête"),
    ("fire_alerts", "alerte incendie", "alertes incendie"),
    ("kobo_forms", "formulaire Kobo", "formulaires Kobo"),
)


def _linked_counts(cooperative_uuid: str) -> Dict[str, int]:
    """What is still attached to the cooperative ({} when nothing)."""
    db = service_db()
    try:
        counts = page_rpc.call(db, "cooperative_linked_counts", {"p_cooperative": cooperative_uuid})
        if isinstance(counts, dict):
            return {k: int(v) for k, v in counts.items() if v}
    except Exception as exc:
        log.warning("cooperative_linked_counts failed, counting table by table: %s", exc)
    counts: Dict[str, int] = {}
    for key, table in (("farmers", "farmers"), ("parcels", "parcels"),
                       ("staff", "user_profiles"), ("forms", "formulaires")):
        try:
            response = (db.table(table).select("cooperative_id", count="exact", head=True)
                        .eq("cooperative_id", cooperative_uuid).execute())
            n = int(getattr(response, "count", None) or 0)
        except Exception:
            n = 0
        if n:
            counts[key] = n
    return counts


def delete_cooperative(uid: str, role: Optional[str], cooperative_uuid: str) -> str:
    """Delete a cooperative: only when it is deactivated and nothing is
    attached to it any more (otherwise ON DELETE CASCADE would wipe its
    farmers and parcels). The database trigger enforces the same rule.
    Returns its name. Raises ServiceError with what blocks the deletion."""
    try:
        db, current = _scoped(uid, role, cooperative_uuid)
    except ServiceError:
        raise
    except Exception as exc:
        log.warning("Cooperative lookup failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc
    name = current.get("name") or ""
    if current.get("is_active", True) is not False:
        raise ServiceError(f"La coopérative « {name} » est active : désactivez-la avant de la supprimer.")
    linked = _linked_counts(cooperative_uuid)
    if linked:
        parts = [f"{n} {plural if n > 1 else single}"
                 for key, single, plural in LINKED_LABELS for n in [linked.get(key, 0)] if n]
        raise ServiceError(f"La coopérative « {name} » ne peut pas être supprimée : "
                           f"{', '.join(parts)} y {'sont' if sum(linked.values()) > 1 else 'est'} "
                           "encore rattaché(e)s. Supprimez-les ou rattachez-les ailleurs d'abord.")
    try:
        deleted = CooperativeRepository(db).delete_by("uuid", cooperative_uuid)
    except Exception as exc:
        log.warning("Cooperative delete failed for %s: %s", cooperative_uuid, exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc
    if not deleted:
        raise ServiceError("Vous n'avez pas l'autorisation de supprimer cette coopérative.")
    personnel_service.invalidate_overviews()
    return name


# --------------------------------------------------------------------------
# Cooperatives
# --------------------------------------------------------------------------
def list_cooperatives() -> List[Row]:
    """The member's cooperatives, newest first."""
    try:
        return CooperativeRepository(user_db()).list_all()
    except Exception as exc:
        log.exception("Could not list cooperatives")
        raise ServiceError("Impossible de charger vos coopératives. Veuillez réessayer.") from exc


@dataclass
class CooperativeListing:
    """The cooperatives page: rows, and for an admin the managers he may
    assign a new cooperative to."""
    cooperatives: List[Row]
    managers: List[Row] = field(default_factory=list)


def list_for_member(uid: str, role: Optional[str]) -> CooperativeListing:
    """Cooperatives the member sees. Admins: their own and their managers',
    each with `manager_id` / `manager_name`."""
    if role != Roles.ADMIN:
        return CooperativeListing(list_cooperatives())
    try:
        page = page_rpc.call(service_db(), "admin_cooperatives_page", {"p_admin": uid})
        if isinstance(page, dict):
            managers = page.get("managers") or []
            names = {m["uuid"]: account_service.manager_label(m) for m in managers}
            rows = page.get("cooperatives") or []
        else:
            # The caller already read the role (cached lookup): no need to
            # load the admin's own profile first.
            managers = account_service.admin_managers(account_service.Actor(uid, role, None))
            names = {m["uuid"]: account_service.manager_label(m) for m in managers}
            rows = CooperativeRepository(service_db()).list_owned_by_any(
                [uid, *names], columns="*")
    except ServiceError:
        raise
    except Exception as exc:
        log.exception("Could not list the cooperatives of admin %s", uid)
        raise ServiceError("Impossible de charger vos coopératives. Veuillez réessayer.") from exc
    rows.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    for c in rows:
        c["manager_id"] = c.get("owner") if c.get("owner") in names else None
        c["manager_name"] = names.get(c.get("owner"))
    return CooperativeListing(
        rows, [{"uuid": i, "name": n} for i, n in sorted(names.items(), key=lambda x: x[1].lower())])


def list_cooperative_names(db=None) -> List[Row]:
    """uuid + name of the member's cooperatives, alphabetical. `db`: the
    member's client, when called outside the request thread."""
    try:
        return CooperativeRepository(db or user_db()).list_names()
    except Exception as exc:
        log.exception("Could not list cooperative names")
        raise ServiceError("Impossible de charger vos coopératives. Veuillez réessayer.") from exc


def register_cooperative(owner_uid: str, data: CooperativeInput,
                         role: Optional[str] = None,
                         manager_id: Optional[str] = None) -> None:
    """Insert a cooperative owned by `owner_uid`. An admin registers it for
    one of his managers (`manager_id`), who becomes its owner. Raises
    ServiceError."""
    row = {**data.to_row(creating=True), "owner": owner_uid, "created_by": owner_uid}
    db = user_db()
    if role == Roles.ADMIN:
        if not manager_id:
            raise ServiceError("Choisissez le gestionnaire responsable de la coopérative.")
        actor = account_service.load_actor(owner_uid)
        if manager_id not in {m["uuid"] for m in account_service.admin_managers(actor)}:
            log.warning("Admin %s tried to give a cooperative to %s", owner_uid, manager_id)
            raise ServiceError("Vous ne pouvez confier une coopérative qu'à l'un de vos gestionnaires.")
        row["owner"] = manager_id
        db = service_db()       # RLS only lets members insert rows they own
    try:
        _write(CooperativeRepository(db).insert, row)
    except Exception as exc:
        log.warning("Cooperative insert failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc
    personnel_service.invalidate_overviews()


def update_cooperative(uid: str, cooperative_uuid: str, data: CooperativeInput,
                       role: Optional[str] = None,
                       manager_id: Optional[str] = None) -> None:
    """Update a cooperative. A manager edits his own (Row Level Security);
    an admin edits his own and his managers', and may hand one over to
    another of his managers - its farmers and parcels follow it, so the new
    manager sees them. Raises ServiceError."""
    values = data.to_row(creating=False)
    personnel_service.invalidate_overviews()
    if role != Roles.ADMIN:
        try:
            repo = CooperativeRepository(user_db())
            updated = _write(lambda row: repo.update(cooperative_uuid, row), values)
        except Exception as exc:
            log.warning("Cooperative update failed: %s", exc)
            raise ServiceError(friendly_db_error(str(exc))) from exc
        if not updated:
            raise ServiceError("Coopérative introuvable.")
        return

    if not manager_id:
        raise ServiceError("Choisissez le gestionnaire responsable de la coopérative.")
    actor = account_service.load_actor(uid)
    managers = {m["uuid"] for m in account_service.admin_managers(actor)}
    if manager_id not in managers:
        raise ServiceError("Vous ne pouvez confier une coopérative qu'à l'un de vos gestionnaires.")
    db = service_db()
    current = next((c for c in CooperativeRepository(db).list_owned_by_any([uid, *managers])
                    if c["uuid"] == cooperative_uuid), None)
    if current is None:
        log.warning("Admin %s tried to edit cooperative %s", uid, cooperative_uuid)
        raise ServiceError("Coopérative introuvable.")
    try:
        repo = CooperativeRepository(db)
        _write(lambda row: repo.update(cooperative_uuid, row), {**values, "owner": manager_id})
        if current.get("owner") != manager_id:
            for table in ("farmers", "parcels"):
                (db.table(table).update({"owner": manager_id})
                 .eq("cooperative_id", cooperative_uuid).execute())
    except Exception as exc:
        log.warning("Cooperative update failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc


# --------------------------------------------------------------------------
# Farmers
# --------------------------------------------------------------------------
def get_farmers_page() -> "tuple[List[Row], List[Row]]":
    """(farmers with their cooperative, cooperative names) of the member:
    one round trip (get_my_farmers_page), or two queries at the same time."""
    db = user_db()
    page = page_rpc.call(db, "get_my_farmers_page")
    if isinstance(page, dict):
        return page.get("farmers") or [], page.get("cooperatives") or []
    return parallel(lambda: list_farmers(db), lambda: list_cooperative_names(db))


def list_farmers(db=None) -> List[Row]:
    """The member's farmers, newest first, with their cooperative name."""
    try:
        return FarmerRepository(db or user_db()).list_with_cooperative()
    except Exception as exc:
        log.exception("Could not list farmers")
        raise ServiceError("Impossible de charger vos producteurs. Veuillez réessayer.") from exc


def register_farmer(owner_uid: str, data: FarmerInput) -> None:
    """Insert a farmer owned by `owner_uid`. Raises ServiceError."""
    ensure_active(data.cooperative_id, "un producteur")
    row = {
        "cooperative_id": data.cooperative_id,
        "full_name": data.full_name,
        "year_of_birth": data.year_of_birth,
        "gender": data.gender,
        "id_card": data.id_card,
        "handicap": data.handicap,
        "marital_status": data.marital_status,
        "household_number": data.household_number,
        "status": data.status,
        "owner": owner_uid,
    }
    try:
        FarmerRepository(user_db()).insert(row)
    except Exception as exc:
        log.warning("Farmer insert failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc


def update_farmer(farmer_uuid: str, data: FarmerInput) -> None:
    """Update the editable columns of a farmer. Raises ServiceError."""
    try:
        FarmerRepository(user_db()).update(farmer_uuid, data.editable_values())
    except Exception as exc:
        log.warning("Farmer update failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc


@dataclass
class DeletedFarmer:
    """What delete_farmer() removed, for the confirmation message."""
    full_name: str
    parcel_count: int


def delete_farmer(farmer_uuid: str) -> DeletedFarmer:
    """Delete one of the member's farmers (Row Level Security: only his
    own). His parcels go with him (parcels.farmer_id ON DELETE CASCADE, and
    their audits / campaign history with them); survey answers that named
    him keep their data and simply lose the link (ON DELETE SET NULL).
    Raises ServiceError."""
    db = user_db()
    try:
        farmer = FarmerRepository(db).get(farmer_uuid)
        if farmer is None:
            raise ServiceError("Ce producteur est introuvable ou a déjà été supprimé.")
        parcel_count = ParcelRepository(db).count_where("farmer_id", farmer_uuid)
        deleted = FarmerRepository(db).delete_by("uuid", farmer_uuid)
    except ServiceError:
        raise
    except Exception as exc:
        log.warning("Farmer delete failed for %s: %s", farmer_uuid, exc)
        if "foreign key" in str(exc).lower():
            raise ServiceError("Ce producteur ne peut pas être supprimé : des données lui sont "
                               "encore rattachées.") from exc
        raise ServiceError(friendly_db_error(str(exc))) from exc
    if not deleted:
        raise ServiceError("Vous n'avez pas l'autorisation de supprimer ce producteur.")
    return DeletedFarmer(full_name=farmer.get("full_name") or "", parcel_count=parcel_count)
