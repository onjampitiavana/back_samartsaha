from typing import Callable

from flask import Blueprint, flash, redirect, render_template, request, url_for

from core.forms.record_forms import (
    CampaignStatusInput, FarmerInput, ParcelReassignInput,
)
from core.models.choices import (
    CAMPAIGN_STATUS_LABELS_FR, GENDER_CHOICES, MARITAL_STATUS_CHOICES, RECORD_STATUS_CHOICES,
)
from core.routes.decorators import login_required
from core.routes.helpers import flash_errors, send_export
from core.services import cooperative_service, data_service, parcel_service
from core.services.data_service import ExportFile
from core.services.errors import ServiceError

data_bp = Blueprint("data", __name__)


def _back_to_data():
    return redirect(url_for("data.data_management"))


@data_bp.route("/data", methods=["GET"])
@login_required
def data_management():
    """Farmers, parcels and bio campaign status in one place."""
    try:
        view = data_service.get_management_view()
    except ServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.dashboard"))
    return render_template(
        "data_management.html",
        farmer_records=view.farmer_records, parcel_records=view.parcel_records,
        coops=view.coops, farmer_options=view.farmer_options,
        campaign_log=view.campaign_log,
        gender_choices=GENDER_CHOICES, marital_status_choices=MARITAL_STATUS_CHOICES,
        status_choices=RECORD_STATUS_CHOICES,
    )


@data_bp.route("/data/farmers/<farmer_uuid>/edit", methods=["POST"])
@login_required
def edit_farmer(farmer_uuid: str):
    """Inline edit of a farmer."""
    parsed = FarmerInput.parse_edit(request.form)
    if not parsed.ok:
        flash_errors(parsed.errors)
        return _back_to_data()
    try:
        cooperative_service.update_farmer(farmer_uuid, parsed.data)
    except ServiceError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Le producteur « {parsed.data.full_name} » a été mis à jour.", "success")
    return _back_to_data()


@data_bp.route("/data/parcels/<parcel_uuid>/edit", methods=["POST"])
@login_required
def edit_parcel(parcel_uuid: str):
    """Move a parcel to another cooperative / farmer."""
    parsed = ParcelReassignInput.parse(request.form)
    if not parsed.ok:
        flash_errors(parsed.errors)
        return _back_to_data()
    try:
        parcel_service.reassign_parcel(parcel_uuid, parsed.data)
    except ServiceError as exc:
        flash(str(exc), "error")
    else:
        flash("Parcelle réaffectée.", "success")
    return _back_to_data()


@data_bp.route("/data/campaign-status", methods=["POST"])
@login_required
def update_campaign_status():
    """Mark a farmer or parcel as considered / banned for the bio campaign."""
    parsed = CampaignStatusInput.parse(request.form)
    if not parsed.ok:
        flash_errors(parsed.errors)
        return _back_to_data()
    try:
        data_service.set_campaign_status(parsed.data)
    except ServiceError as exc:
        flash(str(exc), "error")
    else:
        flash("Statut mis à jour : "
              f"{CAMPAIGN_STATUS_LABELS_FR.get(parsed.data.new_status, parsed.data.new_status)}.", "success")
    return _back_to_data()


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def _export(build: Callable[[], ExportFile]):
    """Serve build()'s file, or go back to the data page with its error."""
    try:
        return send_export(build())
    except ServiceError as exc:
        flash(str(exc), "error")
        return _back_to_data()


@data_bp.route("/data/export/parcels.xlsx")
@login_required
def export_parcels_xlsx():
    """Parcels as an Excel sheet (geometry as WKT)."""
    return _export(data_service.export_parcels_xlsx)


@data_bp.route("/data/export/parcels.kml")
@login_required
def export_parcels_kml():
    """Parcels as KML."""
    return _export(data_service.export_parcels_kml)


@data_bp.route("/data/export/parcels.shp")
@login_required
def export_parcels_shp():
    """Parcels as a zipped Shapefile."""
    return _export(data_service.export_parcels_shapefile)


@data_bp.route("/data/export/farmers.xlsx")
@login_required
def export_farmers_xlsx():
    """Farmers as an Excel sheet."""
    return _export(data_service.export_farmers_xlsx)


@data_bp.route("/data/export/cooperatives.xlsx")
@login_required
def export_cooperatives_xlsx():
    """Cooperatives as an Excel sheet."""
    return _export(data_service.export_cooperatives_xlsx)
