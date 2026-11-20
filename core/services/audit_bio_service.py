"""Audit Bio (web) - organic compliance audits of a manager's parcels,
Regulation (EU) 2018/848 (migrations/20261122_audits_bio_web.sql).

Every query runs with the member's own Supabase client (user_db): Row Level
Security decides what he may read and write. A manager sees the audits of
the parcels he owns, whether he recorded them on the web or an auditor did
on the mobile app. A recorded audit is final (the database refuses any
change but the follow-up of its non-conformities); work in progress lives
in audits_bio_drafts.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from core.forms.audit_bio_forms import UUID_RE, AuditBioInput
from core.models import AuditDraftRepository, AuditRepository
from core.models.base import Row
from core.services import cooperative_service, storage_service
from core.services.clients import user_db
from core.services.concurrency import parallel
from core.services.data_service import ExportFile
from core.services.errors import ServiceError, friendly_db_error

log = logging.getLogger(__name__)

MIGRATION = "20261122_audits_bio_web.sql"
SIGNATURE_DATA_PREFIX = "data:image/"


def _missing_migration(exc: Exception) -> bool:
    text = str(exc)
    return ("PGRST202" in text or "PGRST205" in text
            or ("audits_bio_drafts" in text and "exist" in text))


def _db_error(exc: Exception, what: str) -> ServiceError:
    """Log `exc` and turn it into a message the manager can act on."""
    if _missing_migration(exc):
        log.error("Audit Bio: run migrations/%s in Supabase (%s)", MIGRATION, exc)
        return ServiceError("L'audit bio n'est pas encore installé sur le serveur "
                            f"(migration {MIGRATION}).")
    if "validated audit is locked" in str(exc).lower():
        return ServiceError("Un audit enregistré ne peut plus être modifié : réalisez un nouvel "
                            "audit pour constater une évolution.")
    log.warning("Audit Bio: %s failed: %s", what, exc)
    return ServiceError(friendly_db_error(str(exc)))


def _check_uuid(value: str, message: str) -> str:
    value = (value or "").lower()
    if not UUID_RE.match(value):
        raise ServiceError(message)
    return value


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def page() -> Row:
    """{parcels, audits, drafts} of the Audit Bio page."""
    try:
        data = AuditRepository(user_db()).page()
    except Exception as exc:
        raise _db_error(exc, "page") from exc
    return {"parcels": data.get("parcels") or [], "audits": data.get("audits") or [],
            "drafts": data.get("drafts") or []}


def parcel_context(parcel_id: str) -> Row:
    """Identification of a parcel for a new audit (pre-filled fields) and
    its audit history. Raises ServiceError if the parcel isn't his."""
    parcel_id = _check_uuid(parcel_id, "Cette parcelle est introuvable.")
    db = user_db()
    repo = AuditRepository(db)
    try:
        context, history = parallel(lambda: repo.parcel_context(parcel_id),
                                    lambda: repo.list_for_parcel(parcel_id))
    except Exception as exc:
        raise _db_error(exc, "parcel context") from exc
    if not context:
        raise ServiceError("Cette parcelle est introuvable ou ne fait pas partie de vos "
                           "coopératives.")
    return {**context, "history": history}


def _signature_for_display(value: Optional[str]) -> Optional[str]:
    """A data URL stays as is; a storage path (mobile app) becomes a
    short-lived signed URL."""
    if not value:
        return None
    if value.startswith(SIGNATURE_DATA_PREFIX):
        return value
    return storage_service.signed_urls([value]).get(value)


def get_audit(audit_id: str) -> Row:
    """One audit with its parcel identification and the parcel's history
    (campaign comparison)."""
    audit_id = _check_uuid(audit_id, "Cet audit est introuvable.")
    try:
        audit = AuditRepository(user_db()).get(audit_id)
    except Exception as exc:
        raise _db_error(exc, "audit read") from exc
    if not audit:
        raise ServiceError("Cet audit est introuvable ou ne concerne pas vos parcelles.")
    context = parcel_context(audit["parcel_id"])
    audit["auditor_signature"] = _signature_for_display(audit.get("auditor_signature"))
    return {"audit": audit, **context}


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------
def _draft_data(data: AuditBioInput) -> Row:
    """A draft is never signed: a signature validates the final content."""
    return {k: v for k, v in data.values.items() if k != "auditor_signature"}


def save_draft(data: AuditBioInput, draft_id: Optional[str] = None) -> Row:
    """Create or update one of the member's drafts; returns {uuid, updated_at}."""
    cooperative_service.ensure_parcel_active(data.parcel_id, "un audit")
    repo = AuditDraftRepository(user_db())
    row = {"parcel_id": data.parcel_id, "data": _draft_data(data)}
    try:
        if draft_id:
            draft_id = _check_uuid(draft_id, "Ce brouillon est introuvable.")
            saved = repo.update(draft_id, {**row, "updated_at": datetime.now(timezone.utc).isoformat()})
            if not saved:
                raise ServiceError("Ce brouillon est introuvable ou a déjà été validé.")
            saved_row = saved[0]
        else:
            saved_row = repo.insert(row) or {}
    except ServiceError:
        raise
    except Exception as exc:
        raise _db_error(exc, "draft save") from exc
    return {"uuid": saved_row.get("uuid"), "updated_at": saved_row.get("updated_at")}


def get_draft(draft_id: str) -> Row:
    draft_id = _check_uuid(draft_id, "Ce brouillon est introuvable.")
    try:
        draft = AuditDraftRepository(user_db()).get(draft_id)
    except Exception as exc:
        raise _db_error(exc, "draft read") from exc
    if not draft:
        raise ServiceError("Ce brouillon est introuvable ou a déjà été validé.")
    return draft


def delete_draft(draft_id: str) -> None:
    draft_id = _check_uuid(draft_id, "Ce brouillon est introuvable.")
    try:
        deleted = AuditDraftRepository(user_db()).delete_by("uuid", draft_id)
    except Exception as exc:
        raise _db_error(exc, "draft delete") from exc
    if not deleted:
        raise ServiceError("Ce brouillon est introuvable ou a déjà été supprimé.")


# --------------------------------------------------------------------------
# Recording (validation) and follow-up
# --------------------------------------------------------------------------
def record(owner_uid: str, data: AuditBioInput, draft_id: Optional[str] = None) -> Row:
    """Insert the validated, signed audit (owner = the manager) and drop
    the draft it came from. RLS refuses a parcel outside his scope; the
    database computes the audited area, the campaign and the renewal date
    when they are not given."""
    cooperative_service.ensure_parcel_active(data.values.get("parcel_id"), "un audit")
    repo = AuditRepository(user_db())
    try:
        saved = repo.insert({**data.values, "owner": owner_uid})
    except Exception as exc:
        raise _db_error(exc, "audit insert") from exc
    if not saved or not saved.get("uuid"):
        raise ServiceError("L'audit n'a pas été enregistré. Veuillez réessayer.")
    if draft_id:
        try:
            AuditDraftRepository(user_db()).delete_by("uuid", _check_uuid(draft_id, ""))
        except Exception as exc:          # the audit is saved: the draft is only clutter
            log.warning("Audit Bio: draft %s not removed after validation: %s", draft_id, exc)
    return {"uuid": saved["uuid"], "parcel_id": saved.get("parcel_id")}


def set_non_conformity_resolved(audit_id: str, index: int, resolved: bool) -> List[Row]:
    """Mark one finding of a recorded audit as (un)resolved; returns the
    updated list."""
    audit_id = _check_uuid(audit_id, "Cet audit est introuvable.")
    repo = AuditRepository(user_db())
    try:
        audit = repo.get(audit_id)
    except Exception as exc:
        raise _db_error(exc, "audit read") from exc
    if not audit:
        raise ServiceError("Cet audit est introuvable ou ne concerne pas vos parcelles.")
    items: List[Dict[str, Any]] = list(audit.get("non_conformity_items") or [])
    if not 0 <= index < len(items):
        raise ServiceError("Cette non-conformité est introuvable.")
    items[index] = {**items[index], "resolved": bool(resolved),
                    "resolved_at": date.today().isoformat() if resolved else None}
    try:
        updated = repo.set_non_conformities(audit_id, items)
    except Exception as exc:
        raise _db_error(exc, "non-conformity follow-up") from exc
    if not updated:
        raise ServiceError("Vous n'avez pas l'autorisation de suivre cet audit.")
    return items


def report_pdf(audit_id: str) -> ExportFile:
    """The audit report (PDF) of one audit."""
    from core.services import audit_bio_pdf   # reportlab: loaded on demand

    audit_id = _check_uuid(audit_id, "Cet audit est introuvable.")
    try:
        audit = AuditRepository(user_db()).get(audit_id)
    except Exception as exc:
        raise _db_error(exc, "audit read") from exc
    if not audit:
        raise ServiceError("Cet audit est introuvable ou ne concerne pas vos parcelles.")
    context = parcel_context(audit["parcel_id"])
    signature = audit.get("auditor_signature")
    signature_png = audit_bio_pdf.signature_bytes(signature)
    if signature_png is None and signature and not signature.startswith(SIGNATURE_DATA_PREFIX):
        signature_png = storage_service.download(signature)
    return audit_bio_pdf.build(audit, context, signature_png)
