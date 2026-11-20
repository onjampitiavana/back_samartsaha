"""Parcels (geometry import, map, reassignment) and bio audits of the
signed-in member."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import shapely

from flask import current_app
from werkzeug.utils import secure_filename

from core.forms.record_forms import (
    AuditInput, ParcelDrawInput, ParcelReassignInput, ParcelUploadInput,
)
from core.models import AuditRepository, CooperativeRepository, FarmerRepository, ParcelRepository
from core.models.base import Row
from core.services import cooperative_service, geo_service
from core.services.clients import user_db
from core.services import page_rpc
from core.services.concurrency import Failed, parallel
from core.services.errors import ServiceError, friendly_db_error

log = logging.getLogger(__name__)


@dataclass
class ParcelsPage:
    """Data of the parcels page: cooperatives and farmer options, the parcel
    list, and (HTML page only) the map. The Nuxt page loads the map
    separately (get_parcel_map), only when `map_version` changes."""
    farmers_by_coop: Dict[str, List[Row]]
    parcel_rows: List[Row] = field(default_factory=list)
    feature_collection: Dict[str, Any] = field(default_factory=dict)
    cooperatives: List[Row] = field(default_factory=list)
    map_version: str = ""


def _my_parcels(db=None) -> List[Row]:
    """get_my_parcels(), or [] if the RPC is unavailable."""
    try:
        return ParcelRepository(db or user_db()).list_mine()
    except Exception as exc:
        log.warning("get_my_parcels failed: %s", exc)
        return []


def map_version(rows: List[Row]) -> str:
    """Changes whenever a parcel is added or removed. A parcel's outline is
    never edited once stored, so the same set of parcels means the same map."""
    digest = hashlib.sha1(",".join(sorted(str(r.get("uuid")) for r in rows)).encode())
    return digest.hexdigest()[:16]


def _feature_collection(rows: List[Row]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": json.loads(row["geojson"]),
        "properties": {
            "nom": row.get("nom"),
            "cooperative_name": row.get("cooperative_name"),
            "farmer_name": row.get("farmer_name"),
            "area_ha": row.get("area_ha"),
        },
    } for row in rows if row.get("geojson")]}


def get_parcels_page(with_map: bool = True) -> ParcelsPage:
    """Cooperatives, farmers grouped by cooperative (form) and the parcels,
    read at the same time. `with_map`: also the parcels as GeoJSON (HTML
    page); without it the rows carry no geometry."""
    db = user_db()
    page = None if with_map else page_rpc.call(db, "get_my_parcels_page")
    if isinstance(page, dict):
        coops, farmers, rows = (page.get("cooperatives") or [], page.get("farmers") or [],
                                page.get("parcels") or [])
    else:
        coops, farmers, rows = parallel(
            lambda: CooperativeRepository(db).list_names(),
            lambda: FarmerRepository(db).list_options(),
            lambda: _my_parcels(db),
            return_errors=True)
    for result, what in ((coops, "cooperatives"), (farmers, "farmers")):
        if isinstance(result, Failed):
            log.error("Could not list the %s of the parcels page: %s", what, result.error)
            raise ServiceError("Impossible de charger vos producteurs. Veuillez réessayer.")

    farmers_by_coop: Dict[str, List[Row]] = {}
    for f in farmers:
        farmers_by_coop.setdefault(f["cooperative_id"], []).append(
            {"uuid": f["uuid"], "full_name": f["full_name"]})

    return ParcelsPage(
        farmers_by_coop=farmers_by_coop,
        parcel_rows=rows if with_map else [{k: v for k, v in r.items() if k != "geojson"}
                                           for r in rows],
        feature_collection=_feature_collection(rows) if with_map else {},
        cooperatives=coops,
        map_version=map_version(rows),
    )


# Outlines on the map: ~0.5 m tolerance and 6 decimals (~0.1 m) are
# invisible at any zoom the map offers, and divide the payload of GPS
# traces (one point per second) several times.
MAP_SIMPLIFY_TOLERANCE_DEG = 0.000005
MAP_DECIMALS = 6


def _light_geometry(geojson_text: str) -> Optional[Dict[str, Any]]:
    """A GeoJSON geometry simplified and rounded for display, or None."""
    try:
        geom = shapely.from_geojson(geojson_text)
        light = geom.simplify(MAP_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
        if light.is_empty:
            light = geom
        light = shapely.transform(light, lambda xy: np.round(xy, MAP_DECIMALS))
        return json.loads(shapely.to_geojson(light))
    except Exception as exc:
        log.warning("Unreadable parcel geometry skipped on the map: %s", exc)
        return None


def get_parcel_map() -> Dict[str, Any]:
    """{"version", "geometries": {parcel uuid: GeoJSON geometry}}: the map of
    the parcels page, light enough to be cached per version by the browser."""
    db = user_db()
    light = page_rpc.call(db, "get_my_parcel_map", {"p_tolerance": MAP_SIMPLIFY_TOLERANCE_DEG})
    if isinstance(light, dict):          # simplified by PostGIS already
        return {"version": map_version([{"uuid": k} for k in light]), "geometries": light}

    rows = _my_parcels(db)
    geometries = {}
    for row in rows:
        if row.get("geojson"):
            light = _light_geometry(row["geojson"])
            if light:
                geometries[row["uuid"]] = light
    return {"version": map_version(rows), "geometries": geometries}


@dataclass
class ImportResult:
    """What import_parcels() stored, and the shapes of the file it ignored."""
    count: int
    skipped: List[str] = field(default_factory=list)


# public.insert_parcels (migrations/20261116_performance.sql): None until
# tried, False when the migration has not been run yet.
_batch_rpc_available: Optional[bool] = None


def import_parcels(data: ParcelUploadInput) -> ImportResult:
    """Store every polygon of the uploaded file as a parcel of the chosen
    farmer. All of them in one transaction (insert_parcels) when the
    migration is installed, otherwise one call per shape. Raises
    ServiceError with a message naming the problem."""
    global _batch_rpc_available
    cooperative_service.ensure_active(data.cooperative_id, "une parcelle")
    try:
        features, skipped = geo_service.read_parcel_file(
            data.upload, current_app.config["EXPORT_TMP_DIR"])
    except ValueError as exc:                    # file content problems
        log.info("Parcel import rejected: %s", exc)
        raise ServiceError(str(exc)) from exc
    except Exception as exc:
        log.exception("Parcel file could not be read")
        raise ServiceError("Le fichier n'a pas pu être lu. Vérifiez qu'il s'agit d'un fichier "
                           ".kml, .gpkg ou d'un Shapefile compressé valide.") from exc

    parcels = ParcelRepository(user_db())
    if _batch_rpc_available is not False:
        try:
            count = parcels.insert_geometries(data.cooperative_id, data.farmer_id, features)
            _batch_rpc_available = True
            return ImportResult(count, skipped)
        except Exception as exc:
            if "PGRST202" in str(exc) or "insert_parcels" in str(exc):
                _batch_rpc_available = False
                log.warning("insert_parcels() is missing: run "
                            "migrations/20261116_performance.sql in Supabase. Falling back "
                            "to one call per parcel.")
            else:
                log.warning("Parcel import failed: %s", exc)
                raise ServiceError("Aucune parcelle n'a été importée : "
                                   + _lower_first(friendly_db_error(str(exc)))) from exc

    for index, feat in enumerate(features):
        try:
            parcels.insert_geometry(data.cooperative_id, data.farmer_id,
                                    feat["geometry"], feat.get("nom"))
        except Exception as exc:
            log.warning("Parcel import failed at shape %s: %s", index + 1, exc)
            label = feat.get("nom") or f"forme n° {index + 1}"
            done = (f" Les {index} parcelles précédentes ont été importées." if index > 1 else
                    " La parcelle précédente a été importée." if index == 1 else "")
            raise ServiceError(f"La parcelle « {label} » n'a pas pu être enregistrée : "
                               f"{_lower_first(friendly_db_error(str(exc)))}{done}") from exc
    return ImportResult(len(features), skipped)


def save_drawn_parcel(data: ParcelDrawInput) -> Row:
    """Store a parcel drawn on the map (insert_drawn_parcel: same checks,
    SRID, 2D shape and geodesic area as the file import). Returns the saved
    {uuid, nom, area_ha, kind}. Raises ServiceError."""
    cooperative_service.ensure_active(data.cooperative_id, "une parcelle")
    try:
        saved = ParcelRepository(user_db()).insert_drawn(
            data.cooperative_id, data.farmer_id, data.geometry, data.nom)
    except Exception as exc:
        text = str(exc)
        if "PGRST202" in text or "insert_drawn_parcel" in text:
            log.error("insert_drawn_parcel() is missing: run "
                      "migrations/20261119_parcels_drawn.sql in Supabase.")
            raise ServiceError("Le traçage de parcelle n'est pas encore installé sur le "
                               "serveur. Utilisez l'import de fichier en attendant.") from exc
        if "is not a polygon" in text:
            raise ServiceError("Le tracé n'est pas un polygone de parcelle valide.") from exc
        log.warning("Drawn parcel insert failed: %s", exc)
        raise ServiceError("La parcelle n'a pas été enregistrée : "
                           + _lower_first(friendly_db_error(text))) from exc
    if not saved.get("uuid"):
        raise ServiceError("La parcelle n'a pas été enregistrée. Veuillez réessayer.")
    return saved


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def uploaded_filename(data: ParcelUploadInput) -> str:
    """The sanitised name of the uploaded file, for messages."""
    return secure_filename(data.upload.filename)


def reassign_parcel(parcel_uuid: str, data: ParcelReassignInput) -> None:
    """Move a parcel to another cooperative / farmer. Raises ServiceError."""
    cooperative_service.ensure_active(data.cooperative_id, "une parcelle")
    try:
        ParcelRepository(user_db()).update(parcel_uuid, {
            "cooperative_id": data.cooperative_id,
            "farmer_id": data.farmer_id,
        })
    except Exception as exc:
        log.warning("Parcel update failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc


# --------------------------------------------------------------------------
# Bio audits
# --------------------------------------------------------------------------
def audit_parcel_options(db=None) -> List[Row]:
    """The member's parcels as {uuid, label} for the audit form."""
    return [{
        "uuid": row["uuid"],
        "label": f'{row.get("nom") or "Parcelle"} — {row.get("farmer_name", "")} '
                 f'({row.get("area_ha") or "?"} ha)',
    } for row in _my_parcels(db)]


def list_audits(db=None) -> List[Row]:
    """The member's audits (get_my_audits), or [] if unavailable."""
    try:
        return AuditRepository(db or user_db()).list_mine()
    except Exception as exc:
        log.warning("get_my_audits failed: %s", exc)
        return []


def record_audit(owner_uid: str, data: AuditInput) -> None:
    """Insert a bio audit owned by `owner_uid`. Raises ServiceError."""
    cooperative_service.ensure_parcel_active(data.values.get("parcel_id"), "un audit")
    try:
        AuditRepository(user_db()).insert({**data.values, "owner": owner_uid})
    except Exception as exc:
        log.warning("Audit insert failed: %s", exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc


def delete_parcel(parcel_uuid: str) -> str:
    """Delete one of the member's parcels (Row Level Security: only his
    own); its audits and campaign history go with it (ON DELETE CASCADE).
    Returns the parcel's name. Raises ServiceError."""
    parcels = ParcelRepository(user_db())
    try:
        parcel = parcels.get(parcel_uuid)
        if parcel is None:
            raise ServiceError("Cette parcelle est introuvable ou a déjà été supprimée.")
        deleted = parcels.delete_by("uuid", parcel_uuid)
    except ServiceError:
        raise
    except Exception as exc:
        log.warning("Parcel delete failed for %s: %s", parcel_uuid, exc)
        raise ServiceError(friendly_db_error(str(exc))) from exc
    if not deleted:
        raise ServiceError("Vous n'avez pas l'autorisation de supprimer cette parcelle.")
    return parcel.get("nom") or "Parcelle"
