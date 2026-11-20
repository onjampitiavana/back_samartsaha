"""
Survey forms ("formulaires"): building, publishing, assigning and answering.

Who may do what (the role is always re-read from the database):

  admin / superadmin   build and publish forms of the cooperatives
                       they manage; assign field agents; read every response
  agent                answer the published forms assigned to them; read
                       their own responses

Every query runs with the service client after these checks: the response
tables (schema form_data) are not exposed to the API at all.

Versioning: the builder edits `formulaires.fields` (the working copy).
publish_form() freezes it into version n+1 with its own response table, so
editing a form never touches responses already collected. New responses
always go to the latest version.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from postgrest.exceptions import APIError

from core.forms.formulaire_forms import FormulaireInput, ResponseInput
from core.models import (
    FarmerRepository, FormulaireEnqueteurRepository, FormulaireRepository,
    FormulaireVersionRepository, ParcelRepository, ProfileRepository, Roles,
)
from core.models.base import Row
from core.services import account_service, auth_service, personnel_service, storage_service
from core.services.account_service import Actor
from core.services.clients import service_db
from core.services.data_service import XLSX_MIMETYPE, ExportFile, _rows_to_xlsx
from core.services.errors import ServiceError, sql_message

log = logging.getLogger(__name__)

FORM_ADMINS = (Roles.ADMIN, Roles.SUPERADMIN)
FILE_FIELD_TYPES = ("photo", "signature")


class FormulaireServiceError(ServiceError):
    """Survey form failure with a user-facing message."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_error(exc: Exception, fallback: str) -> FormulaireServiceError:
    """A user-facing error for a failed query or RPC."""
    # Messages raised on purpose by the SQL functions, in French.
    known = sql_message(exc.message) if isinstance(exc, APIError) else None
    if known:
        return FormulaireServiceError(known)
    if isinstance(exc, APIError) and exc.code == "23514":      # check violation
        return FormulaireServiceError("Une réponse est en dehors des valeurs autorisées.")
    if isinstance(exc, APIError) and exc.code == "23502":      # not null
        return FormulaireServiceError("Une réponse obligatoire est manquante.")
    return FormulaireServiceError(fallback)


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------
@dataclass
class AdminScope:
    """An administrator and the cooperatives whose forms they manage."""
    actor: Actor
    cooperatives: List[Row]

    def cooperative(self, cooperative_id: Optional[str]) -> Optional[Row]:
        """The managed cooperative with this uuid, or None."""
        return next((c for c in self.cooperatives
                     if c["uuid"] == cooperative_id), None)


def _admin_scope(uid: str) -> AdminScope:
    # An administrator's role and cooperatives are usually already in memory
    # (role cache, staff-page cache): no round trip before the real query.
    if auth_service.lookup_role(uid) == Roles.ADMIN:
        try:
            overview = personnel_service.get_overview(uid, with_members=False, role=Roles.ADMIN)
        except ServiceError as exc:
            raise FormulaireServiceError(str(exc)) from exc
        return AdminScope(Actor(uid=uid, role=Roles.ADMIN, profile=None), overview.cooperatives)
    return _scope_of(account_service.load_actor(uid))


def _scope_of(actor: Actor) -> AdminScope:
    if actor.role not in FORM_ADMINS:
        raise FormulaireServiceError("Seuls les administrateurs peuvent gérer les formulaires.")
    try:
        cooperatives = account_service.manageable_cooperatives(actor)
    except Exception as exc:
        log.exception("Could not load the cooperatives of %s", actor.uid)
        raise FormulaireServiceError("Impossible de charger vos coopératives. Veuillez "
                                     "réessayer.") from exc
    return AdminScope(actor, cooperatives)


def _load_form(formulaire_id: str) -> Row:
    try:
        form = FormulaireRepository(service_db()).get(formulaire_id)
    except Exception as exc:
        log.exception("Could not load form %s", formulaire_id)
        raise FormulaireServiceError("Impossible de charger le formulaire. Veuillez réessayer.") from exc
    if form is None:
        raise FormulaireServiceError("Ce formulaire n'existe pas ou a été supprimé.")
    return form


def _admin_form(scope: AdminScope, formulaire_id: str) -> Row:
    """The form, if it belongs to one of the admin's cooperatives."""
    form = _load_form(formulaire_id)
    coop = scope.cooperative(form["cooperative_id"])
    if coop is None:
        log.warning("%s tried to open form %s of another cooperative",
                    scope.actor.uid, formulaire_id)
        raise FormulaireServiceError("Ce formulaire n'existe pas ou a été supprimé.")
    form["cooperative_name"] = coop.get("name")
    return form


@dataclass
class Responder:
    """Someone allowed to answer / read the responses of a form."""
    actor: Actor
    form: Row
    is_admin: bool


def _responder(uid: str, formulaire_id: str) -> Responder:
    """An admin of the form's cooperative, or an agent assigned to it."""
    actor = account_service.load_actor(uid)
    if actor.role in FORM_ADMINS:
        return Responder(actor, _admin_form(_scope_of(actor), formulaire_id), True)
    if actor.role == Roles.AGENT:
        form = _load_form(formulaire_id)
        try:
            assigned = FormulaireEnqueteurRepository(service_db()).is_assigned(
                formulaire_id, uid)
        except Exception as exc:
            log.exception("Assignment check failed for %s / %s", uid, formulaire_id)
            raise FormulaireServiceError("Impossible de charger le formulaire. Veuillez "
                                         "réessayer.") from exc
        if assigned and form["status"] == "published":
            return Responder(actor, form, False)
    raise FormulaireServiceError("Ce formulaire ne vous est pas accessible.")


# --------------------------------------------------------------------------
# Building (administrators)
# --------------------------------------------------------------------------
@dataclass
class AdminOverview:
    """The admin's cooperatives and their forms."""
    cooperatives: List[Row]
    forms: List[Row] = field(default_factory=list)


def get_admin_overview(uid: str) -> AdminOverview:
    """Every form of the admin's cooperatives, most recently changed first."""
    scope = _admin_scope(uid)
    names = {c["uuid"]: c.get("name") for c in scope.cooperatives}
    try:
        forms = FormulaireRepository(service_db()).list_by_cooperatives(list(names))
    except Exception as exc:
        log.exception("Could not list the forms of %s", uid)
        raise FormulaireServiceError("Impossible de charger vos formulaires. Veuillez "
                                     "réessayer.") from exc
    for f in forms:
        f["cooperative_name"] = names.get(f["cooperative_id"])
        f["field_count"] = len(f.get("fields") or [])
    return AdminOverview(scope.cooperatives, forms)


def get_builder_context(uid: str, formulaire_id: Optional[str] = None):
    """(cooperatives, form or None) for the builder page."""
    scope = _admin_scope(uid)
    form = _admin_form(scope, formulaire_id) if formulaire_id else None
    return scope.cooperatives, form


def create_form(uid: str, data: FormulaireInput) -> str:
    """Save a new draft form; returns its id."""
    scope = _admin_scope(uid)
    if scope.cooperative(data.cooperative_id) is None:
        raise FormulaireServiceError("Choisissez l'une de vos coopératives.")
    try:
        row = FormulaireRepository(service_db()).insert({
            "title": data.title,
            "description": data.description,
            "cooperative_id": data.cooperative_id,
            "fields": data.fields,
            "created_by": uid,
        })
    except Exception as exc:
        log.exception("Form insert failed for %s", uid)
        raise _db_error(exc, "Le formulaire n'a pas pu être enregistré. Veuillez réessayer.") from exc
    log.info("Form %s created by %s", row["id"], uid)
    return row["id"]


def update_form(uid: str, formulaire_id: str, data: FormulaireInput) -> None:
    """Save the builder's changes to the working copy. Published versions
    and their responses are left untouched until the next publication."""
    scope = _admin_scope(uid)
    form = _admin_form(scope, formulaire_id)
    if (form["current_version"] is not None
            and data.cooperative_id != form["cooperative_id"]):
        raise FormulaireServiceError("Un formulaire publié reste rattaché à sa coopérative : "
                                     "ses réponses concernent les producteurs de celle-ci.")
    if scope.cooperative(data.cooperative_id) is None:
        raise FormulaireServiceError("Choisissez l'une de vos coopératives.")

    db = service_db()
    try:
        changed = False
        if form["current_version"] is not None:
            latest = FormulaireVersionRepository(db).get(formulaire_id,
                                                         form["current_version"])
            changed = latest is None or latest["fields"] != data.fields
        FormulaireRepository(db).update(formulaire_id, {
            "title": data.title,
            "description": data.description,
            "cooperative_id": data.cooperative_id,
            "fields": data.fields,
            "has_draft_changes": changed,
            "updated_at": _now(),
        })
    except Exception as exc:
        log.exception("Form update failed for %s", formulaire_id)
        raise _db_error(exc, "Le formulaire n'a pas pu être enregistré. Veuillez réessayer.") from exc


def publish_form(uid: str, formulaire_id: str) -> int:
    """Publish the working copy as a new version (creates its response
    table). Returns the version number."""
    scope = _admin_scope(uid)
    _admin_form(scope, formulaire_id)
    try:
        version = FormulaireRepository(service_db()).publish(formulaire_id, uid)
    except Exception as exc:
        log.warning("Publishing form %s failed: %s", formulaire_id, exc)
        raise _db_error(exc, "Le formulaire n'a pas pu être publié. "
                             "Veuillez réessayer.") from exc
    log.info("Form %s published as v%s by %s", formulaire_id, version, uid)
    return int(version)


def delete_draft(uid: str, formulaire_id: str) -> str:
    """Delete a form that was never published; returns its title."""
    scope = _admin_scope(uid)
    form = _admin_form(scope, formulaire_id)
    if form["current_version"] is not None:
        raise FormulaireServiceError("Un formulaire publié ne peut pas être supprimé, afin de "
                                     "conserver ses réponses.")
    try:
        FormulaireRepository(service_db()).delete(formulaire_id)
    except Exception as exc:
        log.exception("Delete failed for %s", formulaire_id)
        raise _db_error(exc, "Le formulaire n'a pas pu être supprimé. Veuillez réessayer.") from exc
    return form["title"]


# --------------------------------------------------------------------------
# Detail & assignments (administrators)
# --------------------------------------------------------------------------
# The detail page lists the most recent submissions, at most this many.
MAX_DETAIL_SUBMISSIONS = 500


@dataclass
class FormDetail:
    """Everything the form page shows to an administrator."""
    form: Row
    versions: List[Row]
    agents: List[Row]                  # agents of the form's cooperative
    assigned_ids: List[str]
    stats: Row
    # Published forms: every submission (all versions), newest first, as
    # {id, version, submitted_at, agent_name, answers: [{key, label, type,
    # value}]} in the question order of its own version.
    submissions: List[Row] = field(default_factory=list)
    file_urls: Dict[str, str] = field(default_factory=dict)


def get_form_detail(uid: str, formulaire_id: str) -> FormDetail:
    """Versions, assignable agents, assignments and response counts."""
    scope = _admin_scope(uid)
    form = _admin_form(scope, formulaire_id)
    coop = scope.cooperative(form["cooperative_id"])
    agents = account_service.list_team(scope.actor, [coop], role_name=Roles.AGENT)
    db = service_db()
    try:
        versions = FormulaireVersionRepository(db).list_for(formulaire_id)
        assigned_ids = FormulaireEnqueteurRepository(db).agent_ids(formulaire_id)
        stats = (FormulaireRepository(db).stats(formulaire_id)
                 if versions else {"total": 0, "versions": [], "enqueteurs": []})
    except Exception as exc:
        log.exception("Could not load the detail of form %s", formulaire_id)
        raise FormulaireServiceError("Impossible de charger le formulaire. Veuillez réessayer.") from exc
    counts = {v["version"]: v["total"] for v in stats.get("versions", [])}
    for v in versions:
        v["response_count"] = counts.get(v["version"], 0)
    detail = FormDetail(form, versions, agents, assigned_ids, stats)
    if versions:
        detail.submissions, detail.file_urls = _submissions(formulaire_id, versions, agents)
    return detail


def _submissions(formulaire_id: str, versions: List[Row],
                 agents: List[Row]) -> "tuple[List[Row], Dict[str, str]]":
    """Every response of a form, all versions, newest first, each with the
    questions of the version it answered (see FormDetail.submissions)."""
    db = service_db()
    names = {a["uuid"]: a.get("full_name") or a.get("username") or a.get("email")
             for a in agents}
    submissions: List[Row] = []
    file_paths: List[str] = []
    try:
        for version in versions:                  # newest version first
            if not version.get("response_count"):
                continue                          # no query for empty versions
            fields = version["fields"]
            for row in FormulaireRepository(db).responses(formulaire_id, version["version"]):
                answers = [{"key": f["key"], "label": f["label"], "type": f["type"],
                            "min": f.get("min"), "max": f.get("max"),
                            "value": row.get(f["key"])} for f in fields]
                file_paths += [a["value"] for a in answers
                               if a["type"] in FILE_FIELD_TYPES and a["value"]]
                submissions.append({
                    "id": row.get("id"), "version": version["version"],
                    "submitted_at": row.get("submitted_at"),
                    "agent_id": row.get("enqueteur_id"),
                    "agent_name": names.get(row.get("enqueteur_id")),
                    "answers": answers,
                })
    except Exception as exc:
        log.exception("Could not load the responses of form %s", formulaire_id)
        raise FormulaireServiceError("Impossible de charger les réponses. Veuillez réessayer.") from exc

    submissions.sort(key=lambda s: s.get("submitted_at") or "", reverse=True)
    submissions = submissions[:MAX_DETAIL_SUBMISSIONS]
    # Agents who left the cooperative: their name from their profile.
    missing = {s["agent_id"] for s in submissions if s["agent_id"] and not s["agent_name"]}
    if missing:
        try:
            for p in ProfileRepository(db).list_by_uuids(sorted(missing)):
                for s in submissions:
                    if s["agent_id"] == p["uuid"]:
                        s["agent_name"] = p.get("full_name") or p.get("username") or p.get("email")
        except Exception:
            log.exception("Could not name the agents of form %s", formulaire_id)
    for s in submissions:
        s["agent_name"] = s["agent_name"] or "Agent inconnu"
    kept = {a["value"] for s in submissions for a in s["answers"]
            if a["type"] in FILE_FIELD_TYPES and a["value"]}
    return submissions, storage_service.signed_urls(p for p in file_paths if p in kept)


def assign_agents(uid: str, formulaire_id: str, agent_ids: List[str]) -> int:
    """Make `agent_ids` the field agents of the form; returns their number.
    Only agents of the form's own cooperative can be assigned."""
    scope = _admin_scope(uid)
    form = _admin_form(scope, formulaire_id)
    coop = scope.cooperative(form["cooperative_id"])
    allowed = {a["uuid"] for a in account_service.list_team(
        scope.actor, [coop], role_name=Roles.AGENT)}
    wanted = set(agent_ids)
    if not wanted <= allowed:
        log.warning("%s tried to assign agents outside cooperative %s to %s",
                    uid, form["cooperative_id"], formulaire_id)
        raise FormulaireServiceError("Seuls les agents de terrain de la coopérative du "
                                     "formulaire peuvent y être assignés.")
    try:
        FormulaireEnqueteurRepository(service_db()).replace(
            formulaire_id, sorted(wanted), uid)
    except Exception as exc:
        log.exception("Assignment update failed for %s", formulaire_id)
        raise _db_error(exc, "Les agents de terrain n'ont pas pu être enregistrés. "
                             "Veuillez réessayer.") from exc
    return len(wanted)


# --------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------
def list_agent_forms(uid: str) -> List[Row]:
    """The published forms assigned to a field agent."""
    actor = account_service.load_actor(uid)
    if actor.role != Roles.AGENT:
        raise FormulaireServiceError("Seuls les agents de terrain ont des formulaires assignés.")
    db = service_db()
    try:
        ids = FormulaireEnqueteurRepository(db).formulaire_ids(uid)
        forms = FormulaireRepository(db).list_published_by_ids(ids)
    except Exception as exc:
        log.exception("Could not list the forms of agent %s", uid)
        raise FormulaireServiceError("Impossible de charger vos formulaires. Veuillez "
                                     "réessayer.") from exc
    for f in forms:
        f["field_count"] = len(f.get("fields") or [])
    return forms


@dataclass
class FillContext:
    """What the answer page needs: the latest version and the farmers /
    parcels of the form's cooperative."""
    form: Row
    version: Row
    farmers: List[Row]
    parcels: List[Row]


def get_fill_context(uid: str, formulaire_id: str) -> FillContext:
    """The latest published version of a form, ready to be answered."""
    responder = _responder(uid, formulaire_id)
    form = responder.form
    if form["status"] != "published" or form["current_version"] is None:
        raise FormulaireServiceError("Ce formulaire n'accepte pas de réponses pour le moment.")
    db = service_db()
    try:
        version = FormulaireVersionRepository(db).get(formulaire_id,
                                                      form["current_version"])
        farmers = FarmerRepository(db).list_by_cooperative(form["cooperative_id"])
        parcels = ParcelRepository(db).list_by_cooperative(form["cooperative_id"])
    except Exception as exc:
        log.exception("Could not prepare form %s for answering", formulaire_id)
        raise FormulaireServiceError("Impossible de charger le formulaire. Veuillez réessayer.") from exc
    if version is None:
        raise FormulaireServiceError("Ce formulaire n'accepte pas de réponses pour le moment.")
    return FillContext(form, version, farmers, parcels)


def submit_response(uid: str, formulaire_id: str, version: int,
                    data: ResponseInput) -> str:
    """Store a response under the signed-in member's identity; returns its
    id. Refused if the form was republished since the page was opened."""
    responder = _responder(uid, formulaire_id)
    form = responder.form
    if form["status"] != "published":
        raise FormulaireServiceError("Ce formulaire n'accepte pas de réponses pour le moment.")
    if form["current_version"] != version:
        raise FormulaireServiceError("Le formulaire a été modifié pendant votre saisie. "
                                     "Veuillez remplir la nouvelle version.")

    stored_paths = []
    try:
        for upload in data.uploads:
            path = f"{formulaire_id}/v{version}/{uuid.uuid4().hex}.{upload.extension}"
            storage_service.upload(path, upload.content, upload.content_type)
            stored_paths.append(path)
            data.values[upload.key] = path
        response_id = FormulaireRepository(service_db()).insert_response(
            formulaire_id, version, uid, data.farmer_id, data.parcel_id,
            data.values)
    except Exception as exc:
        storage_service.remove(stored_paths)
        if isinstance(exc, ServiceError):
            raise
        log.warning("Response insert failed for form %s: %s", formulaire_id, exc)
        raise _db_error(exc, "La réponse n'a pas pu être enregistrée. "
                             "Veuillez réessayer.") from exc
    log.info("Response %s to form %s v%s by %s", response_id, formulaire_id,
             version, uid)
    return response_id


# --------------------------------------------------------------------------
# Reading responses
# --------------------------------------------------------------------------
@dataclass
class ResponsesView:
    """Responses of one version of a form."""
    form: Row
    versions: List[Row]
    version: Optional[Row]
    rows: List[Row]
    is_admin: bool
    agents: List[Row] = field(default_factory=list)   # filter options (admins)
    enqueteur_id: Optional[str] = None
    file_urls: Dict[str, str] = field(default_factory=dict)


def _load_responses(uid: str, formulaire_id: str, version_number: Optional[int],
                    enqueteur_id: Optional[str]) -> ResponsesView:
    responder = _responder(uid, formulaire_id)
    # Agents only ever see their own responses.
    if not responder.is_admin:
        enqueteur_id = uid
    db = service_db()
    try:
        versions = FormulaireVersionRepository(db).list_for(formulaire_id)
        version = next((v for v in versions if v["version"] == version_number),
                       versions[0] if versions else None)
        rows = (FormulaireRepository(db).responses(formulaire_id, version["version"],
                                                   enqueteur_id)
                if version else [])
    except Exception as exc:
        log.exception("Could not load the responses of form %s", formulaire_id)
        raise FormulaireServiceError("Impossible de charger les réponses. Veuillez réessayer.") from exc

    try:
        parcel_names = ParcelRepository(db).name_lookup(
            sorted({r["parcel_id"] for r in rows if r.get("parcel_id")}))
    except Exception as exc:
        log.warning("Parcel names of form %s unavailable: %s", formulaire_id, exc)
        parcel_names = {}
    for r in rows:
        r["parcel_name"] = parcel_names.get(r.get("parcel_id"))

    agents = []
    if responder.is_admin:
        coop = {"uuid": responder.form["cooperative_id"],
                "name": responder.form.get("cooperative_name")}
        agents = account_service.list_team(responder.actor, [coop],
                                           role_name=Roles.AGENT)
    return ResponsesView(responder.form, versions, version, rows,
                         responder.is_admin, agents, enqueteur_id)


def get_responses_view(uid: str, formulaire_id: str,
                       version_number: Optional[int] = None,
                       enqueteur_id: Optional[str] = None) -> ResponsesView:
    """Responses of one version (default: the latest), with signed URLs for
    their photos and signatures."""
    view = _load_responses(uid, formulaire_id, version_number, enqueteur_id)
    if view.version:
        file_keys = [f["key"] for f in view.version["fields"]
                     if f["type"] in FILE_FIELD_TYPES]
        view.file_urls = storage_service.signed_urls(
            r.get(k) for r in view.rows for k in file_keys)
    return view


def _export_value(f: Row, value: Any) -> Any:
    if value is None:
        return None
    if f["type"] == "multiple_choice":
        return ", ".join(value)
    if f["type"] == "geolocation":
        lng, lat = (value.get("coordinates") or [None, None])[:2]
        return f"{lat}, {lng}"
    if f["type"] == "geoshape":
        ring = (value.get("coordinates") or [[]])[0]
        return "POLYGON((" + ", ".join(f"{lng} {lat}" for lng, lat in ring) + "))"
    return value


def export_responses_xlsx(uid: str, formulaire_id: str,
                          version_number: Optional[int] = None,
                          enqueteur_id: Optional[str] = None) -> ExportFile:
    """Responses of one version as an Excel sheet."""
    view = _load_responses(uid, formulaire_id, version_number, enqueteur_id)
    if not view.version or not view.rows:
        raise FormulaireServiceError("Aucune réponse à exporter.")
    fields = view.version["fields"]
    table = []
    for r in view.rows:
        line = {"response_id": r["id"], "submitted_at": r["submitted_at"],
                "enqueteur": r.get("enqueteur_name"), "enqueteur_id": r["enqueteur_id"],
                "farmer": r.get("farmer_name"), "farmer_id": r.get("farmer_id"),
                "parcel": r.get("parcel_name"), "parcel_id": r.get("parcel_id")}
        for f in fields:
            header = f["label"] if f["label"] not in line else f"{f['label']} ({f['key']})"
            line[header] = _export_value(f, r.get(f["key"]))
        table.append(line)
    filename = f"responses_{formulaire_id[:8]}_v{view.version['version']}.xlsx"
    return ExportFile(_rows_to_xlsx(table), filename, XLSX_MIMETYPE)
