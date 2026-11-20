from flask import (
    Blueprint, flash, g, redirect, render_template, request, url_for,
)

from core.forms.record_forms import (
    AuditInput, CooperativeInput, FarmerInput, ParcelUploadInput,
)
from core.models.choices import (
    AUDIT_STATUS_CHOICES, CONTAMINATION_RISK_CHOICES, CONVERSION_STATUS_CHOICES,
    CROPS, GENDER_CHOICES, LEGAL_STATUS_CHOICES, MARITAL_STATUS_CHOICES,
    NON_CONFORMITY_SEVERITY_CHOICES, RECORD_STATUS_CHOICES,
)
from core.routes.decorators import login_required
from core.routes.helpers import flash_errors
from core.services import cooperative_service, parcel_service, profile_service
from core.services.errors import ServiceError

main_bp = Blueprint("main", __name__)


@main_bp.route("/dashboard")
@login_required
def dashboard():
    """Overview: the member's profile and record counts."""
    overview = profile_service.get_overview(g.uid)
    flash_errors(overview.warnings)
    return render_template("dashboard.html", profile=overview.profile,
                           counts=overview.counts)


@main_bp.route("/cooperatives", methods=["GET", "POST"])
@login_required
def cooperatives():
    """List and register cooperatives."""
    if request.method == "POST":
        parsed = CooperativeInput.parse(request.form)
        if not parsed.ok:
            flash_errors(parsed.errors)
        else:
            try:
                cooperative_service.register_cooperative(g.uid, parsed.data)
            except ServiceError as exc:
                flash(str(exc), "error")
            else:
                flash(f"La coopérative « {parsed.data.name} » a été enregistrée.", "success")
                return redirect(url_for("main.cooperatives"))

    try:
        coops = cooperative_service.list_cooperatives()
    except ServiceError as exc:
        flash(str(exc), "error")
        coops = []
    return render_template("cooperatives.html", coops=coops, crops=CROPS,
                           legal_status_choices=LEGAL_STATUS_CHOICES,
                           status_choices=RECORD_STATUS_CHOICES)


@main_bp.route("/farmers", methods=["GET", "POST"])
@login_required
def farmers():
    """List and register farmers (needs at least one cooperative)."""
    try:
        coops = cooperative_service.list_cooperative_names()
    except ServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.dashboard"))
    if not coops:
        flash("Créez d'abord une coopérative.", "error")
        return redirect(url_for("main.cooperatives"))

    if request.method == "POST":
        parsed = FarmerInput.parse_new(request.form)
        if not parsed.ok:
            flash_errors(parsed.errors)
        else:
            try:
                cooperative_service.register_farmer(g.uid, parsed.data)
            except ServiceError as exc:
                flash(str(exc), "error")
            else:
                flash(f"Le producteur « {parsed.data.full_name} » a été enregistré.", "success")
                return redirect(url_for("main.farmers"))

    try:
        farmer_rows = cooperative_service.list_farmers()
    except ServiceError as exc:
        flash(str(exc), "error")
        farmer_rows = []
    return render_template("farmers.html", farmer_rows=farmer_rows, coops=coops,
                           gender_choices=GENDER_CHOICES,
                           marital_status_choices=MARITAL_STATUS_CHOICES,
                           status_choices=RECORD_STATUS_CHOICES)


@main_bp.route("/parcels", methods=["GET", "POST"])
@login_required
def parcels():
    """Import parcel boundaries and show them on a map."""
    if request.method == "POST":
        parsed = ParcelUploadInput.parse(request.form, request.files)
        if not parsed.ok:
            flash_errors(parsed.errors)
        else:
            try:
                count = parcel_service.import_parcels(parsed.data).count
            except ServiceError as exc:
                flash(str(exc), "error")
            else:
                flash(f"{count} parcelle{'s' if count > 1 else ''} "
                      f"importée{'s' if count > 1 else ''} depuis « "
                      f"{parcel_service.uploaded_filename(parsed.data)} ».", "success")
                return redirect(url_for("main.parcels"))

    try:
        page = parcel_service.get_parcels_page()
        coops = page.cooperatives
    except ServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.dashboard"))
    return render_template("parcels.html", coops=coops,
                           farmers_by_coop=page.farmers_by_coop,
                           parcel_rows=page.parcel_rows,
                           parcels_feature_collection=page.feature_collection)


@main_bp.route("/audits", methods=["GET", "POST"])
@login_required
def audits():
    """Record and list bio audits of the member's parcels."""
    if request.method == "POST":
        parsed = AuditInput.parse(request.form)
        if not parsed.ok:
            flash_errors(parsed.errors)
        else:
            try:
                parcel_service.record_audit(g.uid, parsed.data)
            except ServiceError as exc:
                flash(str(exc), "error")
            else:
                flash("Audit enregistré.", "success")
                return redirect(url_for("main.audits"))

    return render_template(
        "audit.html",
        parcel_options=parcel_service.audit_parcel_options(),
        audit_rows=parcel_service.list_audits(),
        conversion_status_choices=CONVERSION_STATUS_CHOICES,
        contamination_risk_choices=CONTAMINATION_RISK_CHOICES,
        non_conformity_severity_choices=NON_CONFORMITY_SEVERITY_CHOICES,
        overall_status_choices=AUDIT_STATUS_CHOICES,
    )
