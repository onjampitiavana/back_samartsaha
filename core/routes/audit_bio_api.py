"""/api/audits-bio - Audit Bio of the manager's parcels (Regulation (EU)
2018/848). Access: managers only (re-read from the database on every
call); the data itself is filtered by Row Level Security
(migrations/20261122_audits_bio_web.sql)."""

from flask import g, request

from core.forms.audit_bio_forms import AuditBioInput
from core.models import Roles
from core.models.choices import (
    AUDIT_STATUS_CHOICES, CONTAMINATION_RISK_CHOICES, CONVERSION_STATUS_CHOICES,
    CROP_CYCLE_CHOICES, NC_SEVERITY_CHOICES, SEED_CATEGORY_CHOICES,
)
from core.routes.api_bp import _fail, _invalid, _ok, api_bp, api_role_required
from core.routes.helpers import send_export
from core.services import audit_bio_service

AUDIT_ROLES = (Roles.MANAGER,)

CHOICES = {
    "conversion_status": CONVERSION_STATUS_CHOICES,
    "crop_cycle": CROP_CYCLE_CHOICES,
    "seed_category": SEED_CATEGORY_CHOICES,
    "contamination_risk": CONTAMINATION_RISK_CHOICES,
    "nc_severity": NC_SEVERITY_CHOICES,
    "overall_status": AUDIT_STATUS_CHOICES,
}


def _body() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


@api_bp.get("/audits-bio")
@api_role_required(*AUDIT_ROLES)
def audits_bio_page():
    """Tracking table: parcels, every audit of them, the member's drafts."""
    response, status = _ok(choices=CHOICES, **audit_bio_service.page())
    response.headers["Cache-Control"] = "private, no-cache"
    return response, status


@api_bp.get("/audits-bio/parcels/<parcel_id>")
@api_role_required(*AUDIT_ROLES)
def audits_bio_parcel(parcel_id: str):
    """Pre-filled identification of a parcel and its audit history."""
    return _ok(choices=CHOICES, **audit_bio_service.parcel_context(parcel_id))


@api_bp.get("/audits-bio/<audit_id>")
@api_role_required(*AUDIT_ROLES)
def audits_bio_detail(audit_id: str):
    return _ok(choices=CHOICES, **audit_bio_service.get_audit(audit_id))


@api_bp.get("/audits-bio/<audit_id>/pdf")
@api_role_required(*AUDIT_ROLES)
def audits_bio_pdf(audit_id: str):
    return send_export(audit_bio_service.report_pdf(audit_id))


@api_bp.post("/audits-bio")
@api_role_required(*AUDIT_ROLES)
def audits_bio_record():
    """Validate and record a complete, signed audit. Body: the form, plus
    an optional draft_id (the draft is removed once the audit is saved)."""
    body = _body()
    parsed = AuditBioInput.parse(body, final=True)
    if not parsed.ok:
        return _invalid(parsed.errors)
    saved = audit_bio_service.record(g.uid, parsed.data, body.get("draft_id") or None)
    return _ok("Audit validé et enregistré.", 201, audit=saved)


@api_bp.post("/audits-bio/<audit_id>/non-conformities/<int:index>")
@api_role_required(*AUDIT_ROLES)
def audits_bio_follow_up(audit_id: str, index: int):
    """Body {resolved: bool}: follow-up of one finding's action plan."""
    resolved = _body().get("resolved")
    if not isinstance(resolved, bool):
        return _fail("Requête invalide : rechargez la page puis réessayez.", 400)
    items = audit_bio_service.set_non_conformity_resolved(audit_id, index, resolved)
    return _ok("Non-conformité levée." if resolved else "Non-conformité rouverte.",
               non_conformity_items=items)


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------
@api_bp.post("/audits-bio/drafts")
@api_role_required(*AUDIT_ROLES)
def audits_bio_draft_create():
    parsed = AuditBioInput.parse(_body(), final=False)
    if not parsed.ok:
        return _invalid(parsed.errors)
    return _ok("Brouillon enregistré.", 201, draft=audit_bio_service.save_draft(parsed.data))


@api_bp.get("/audits-bio/drafts/<draft_id>")
@api_role_required(*AUDIT_ROLES)
def audits_bio_draft(draft_id: str):
    return _ok(draft=audit_bio_service.get_draft(draft_id))


@api_bp.put("/audits-bio/drafts/<draft_id>")
@api_role_required(*AUDIT_ROLES)
def audits_bio_draft_update(draft_id: str):
    parsed = AuditBioInput.parse(_body(), final=False)
    if not parsed.ok:
        return _invalid(parsed.errors)
    return _ok("Brouillon enregistré.", draft=audit_bio_service.save_draft(parsed.data, draft_id))


@api_bp.delete("/audits-bio/drafts/<draft_id>")
@api_role_required(*AUDIT_ROLES)
def audits_bio_draft_delete(draft_id: str):
    audit_bio_service.delete_draft(draft_id)
    return _ok("Brouillon supprimé.")
