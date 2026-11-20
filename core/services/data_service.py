"""Data management page: overview tables, bio campaign status, exports."""

import io
import logging
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List

import pandas as pd
from flask import current_app

from core.forms.record_forms import CampaignStatusInput
from core.models import (
    CampaignRepository, CooperativeRepository, FarmerRepository,
    ParcelRepository,
)
from core.models.base import Row
from core.services import geo_service
from core.services.clients import user_db
from core.services import page_rpc
from core.services.concurrency import Failed, parallel
from core.services.errors import ServiceError, friendly_db_error

log = logging.getLogger(__name__)

XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass
class ManagementView:
    """Everything the data management page displays."""
    farmer_records: List[Row]
    parcel_records: List[Row]
    coops: List[Row]
    farmer_options: List[Row]
    campaign_log: List[Row] = field(default_factory=list)


@dataclass
class ExportFile:
    """An in-memory file ready to be downloaded."""
    content: bytes
    filename: str
    mimetype: str


def _management_queries(db):
    """The data page's four queries, at the same time (without the
    get_my_data_page migration)."""
    coop_lookup, farmers, parcels, campaign_log = parallel(
        lambda: CooperativeRepository(db).name_lookup(),
        lambda: FarmerRepository(db).list_by_name(),
        lambda: ParcelRepository(db).list_for_management(),
        lambda: CampaignRepository(db).log(),
        return_errors=True)
    for result in (coop_lookup, farmers, parcels):
        if isinstance(result, Failed):
            log.error("Could not load the data management page: %s", result.error)
            raise ServiceError("Impossible de charger vos données. Veuillez réessayer.")
    if isinstance(campaign_log, Failed):
        log.warning("get_campaign_log failed: %s", campaign_log.error)
        campaign_log = []
    return coop_lookup, farmers, parcels, campaign_log


def get_management_view() -> ManagementView:
    """Farmers and parcels with their cooperative / farmer names."""
    db = user_db()
    page = page_rpc.call(db, "get_my_data_page")
    if isinstance(page, dict):
        coop_lookup, farmers, parcels, campaign_log = (
            {c["uuid"]: c["name"] for c in page.get("cooperatives") or []},
            page.get("farmers") or [], page.get("parcels") or [], page.get("campaign_log") or [])
    else:
        coop_lookup, farmers, parcels, campaign_log = _management_queries(db)

    for f in farmers:
        f["cooperative_name"] = coop_lookup.get(f.get("cooperative_id"))
    farmer_lookup = {f["uuid"]: f["full_name"] for f in farmers}
    for p in parcels:
        p["cooperative_name"] = coop_lookup.get(p.get("cooperative_id"))
        p["farmer_name"] = farmer_lookup.get(p.get("farmer_id"))

    return ManagementView(
        farmer_records=farmers,
        parcel_records=parcels,
        coops=[{"uuid": k, "name": v} for k, v in coop_lookup.items()],
        farmer_options=[{"uuid": k, "name": v} for k, v in farmer_lookup.items()],
        campaign_log=campaign_log,
    )


def set_campaign_status(data: CampaignStatusInput) -> None:
    """Mark a farmer / parcel as considered or banned. Raises ServiceError."""
    try:
        CampaignRepository(user_db()).set_status(
            data.entity_type, data.entity_id, data.new_status, data.reason)
    except Exception as exc:
        log.warning("set_campaign_status failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
def _rows_to_xlsx(rows: List[Row]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def _load(loader: Callable[[], List[Row]], empty_message: str) -> List[Row]:
    try:
        rows = loader()
    except Exception as exc:
        log.exception("Export query failed")
        raise ServiceError("L'export n'a pas pu être préparé. Veuillez réessayer.") from exc
    if not rows:
        raise ServiceError(empty_message)
    return rows


def _parcels_gdf():
    try:
        gdf = geo_service.parcels_geodataframe(ParcelRepository(user_db()).list_mine())
    except Exception as exc:
        log.exception("Parcel export query failed")
        raise ServiceError("L'export n'a pas pu être préparé. Veuillez réessayer.") from exc
    if gdf is None:
        raise ServiceError("Aucune parcelle à exporter.")
    return gdf


def export_farmers_xlsx() -> ExportFile:
    """Farmers as an Excel sheet. Raises ServiceError if there are none."""
    rows = _load(FarmerRepository(user_db()).list_by_name, "No farmers to export.")
    return ExportFile(_rows_to_xlsx(rows), "farmers.xlsx", XLSX_MIMETYPE)


def export_cooperatives_xlsx() -> ExportFile:
    """Cooperatives as an Excel sheet. Raises ServiceError if there are none."""
    rows = _load(CooperativeRepository(user_db()).list_for_export,
                 "No cooperatives to export.")
    return ExportFile(_rows_to_xlsx(rows), "cooperatives.xlsx", XLSX_MIMETYPE)


def export_parcels_xlsx() -> ExportFile:
    """Parcels as an Excel sheet, geometry as WKT. Raises ServiceError if there are none."""
    gdf = _parcels_gdf()
    df = gdf.drop(columns="geometry").copy()
    df["wkt"] = gdf.geometry.apply(lambda geom: geom.wkt)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return ExportFile(buf.getvalue(), "parcels.xlsx", XLSX_MIMETYPE)


def export_parcels_kml() -> ExportFile:
    """Parcels as KML. Raises ServiceError if there are none."""
    return ExportFile(geo_service.build_kml(_parcels_gdf()), "parcels.kml",
                      "application/vnd.google-earth.kml+xml")


def export_parcels_shapefile() -> ExportFile:
    """Parcels as a zipped Shapefile. Raises ServiceError if there are none."""
    gdf = _parcels_gdf()
    with tempfile.TemporaryDirectory(dir=current_app.config["EXPORT_TMP_DIR"]) as tmp:
        zip_path = geo_service.build_shapefile_zip(gdf, tmp)
        with open(zip_path, "rb") as f:
            content = f.read()
    # The temp directory is fully cleaned up, with no open handles left -
    # safe on Windows; the zip is served from memory.
    return ExportFile(content, "parcels_shapefile.zip", "application/zip")
