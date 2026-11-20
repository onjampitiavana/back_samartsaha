"""Interactive map dashboard (Tableau de bord cartographique).

One SQL call (dashboard_member_map, migrations/20261121_dashboard_map.sql)
returns every cooperative, farmer and parcel the member may see. The scope is
decided in SQL from the uid of the verified session (fire_member_cooperatives):

  admin    his own cooperatives and those of every manager he supervises
  manager  only the cooperatives assigned to him (cooperative.owner)

This module turns the rows into GeoJSON FeatureCollections, applies the
optional filters and computes the consolidated indicators. The Nuxt page
downloads the whole scope once and filters in the browser (instant); the same
filters are accepted here so the API can also be queried on its own.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.services.clients import service_db
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

# ~2 m: invisible on the dashboard's zoom levels, several times lighter.
SIMPLIFY_TOLERANCE_DEG = 0.00002

UNSET = "__none__"          # filter value for "region / commune / status not set"

_MISSING = ("Le tableau de bord cartographique n'est pas encore installé : exécutez "
            "migrations/20261121_dashboard_map.sql dans Supabase.")

Row = Dict[str, Any]


@dataclass(frozen=True)
class DashboardFilters:
    """Optional filters (query string). None: no filter on that field."""
    cooperative: Optional[str] = None
    crop: Optional[str] = None
    status: Optional[str] = None
    region: Optional[str] = None
    commune: Optional[str] = None

    @classmethod
    def from_args(cls, args: Any) -> "DashboardFilters":
        def value(key: str) -> Optional[str]:
            v = (args.get(key) or "").strip()
            return v or None
        return cls(value("cooperative"), value("crop"), value("status"),
                   value("region"), value("commune"))

    def active(self) -> bool:
        return any((self.cooperative, self.crop, self.status, self.region, self.commune))


def _load(uid: str) -> Row:
    try:
        data = service_db().rpc("dashboard_member_map", {
            "p_uid": uid, "p_tolerance": SIMPLIFY_TOLERANCE_DEG}).execute().data
    except ServiceError:
        raise
    except Exception as exc:
        text = str(exc)
        if "PGRST202" in text or "Could not find the function" in text:
            log.error("dashboard_member_map() is missing: run migrations/20261121_dashboard_map.sql")
            raise ServiceError(_MISSING) from exc
        log.warning("dashboard_member_map failed: %s", exc)
        raise ServiceError("Les données du tableau de bord sont momentanément indisponibles. "
                           "Réessayez dans un instant.") from exc
    return data if isinstance(data, dict) else {}


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _matches(value: Optional[str], wanted: Optional[str]) -> bool:
    if wanted is None:
        return True
    if wanted == UNSET:
        return not value
    return _same(value, wanted)


def _filter(data: Row, f: DashboardFilters) -> Row:
    """Same rules as the Nuxt page (components/dashboard/DashboardMap)."""
    coops = [c for c in data["cooperatives"]
             if (f.cooperative is None or c["id"] == f.cooperative)
             and (f.crop is None or any(_same(x, f.crop) for x in c.get("crops") or []))
             and _matches(c.get("region"), f.region)
             and _matches(c.get("commune"), f.commune)]
    ids = {c["id"] for c in coops}
    parcels = [p for p in data["parcels"]
               if p["cooperative_id"] in ids and _matches(p.get("status"), f.status)]
    farmers = [x for x in data["farmers"] if x["cooperative_id"] in ids]
    if f.status is not None:
        # Only the farmers / cooperatives that have a parcel with that status.
        farmers_with = {p["farmer_id"] for p in parcels}
        coops_with = {p["cooperative_id"] for p in parcels}
        farmers = [x for x in farmers if x["id"] in farmers_with]
        coops = [c for c in coops if c["id"] in coops_with]
    return {"cooperatives": coops, "farmers": farmers, "parcels": parcels}


def _stats(data: Row) -> Row:
    """Indicators of the (filtered) scope. A parcel's crop is the main crop
    of its cooperative (parcels have no crop of their own)."""
    main_crop = {c["id"]: ((c.get("crops") or [None])[0]) for c in data["cooperatives"]}
    by_crop: Dict[str, Row] = {}
    total = 0.0
    for p in data["parcels"]:
        area = float(p.get("area_ha") or 0)
        total += area
        crop = main_crop.get(p["cooperative_id"]) or "Non renseignée"
        entry = by_crop.setdefault(crop, {"crop": crop, "parcels": 0, "area_ha": 0.0})
        entry["parcels"] += 1
        entry["area_ha"] += area
    crops = sorted(by_crop.values(), key=lambda e: (-e["area_ha"], -e["parcels"]))
    for e in crops:
        e["area_ha"] = round(e["area_ha"], 4)
    return {
        "cooperatives": len(data["cooperatives"]),
        "farmers": len(data["farmers"]),
        "parcels": len(data["parcels"]),
        "area_ha": round(total, 4),
        "by_crop": crops,
    }


def _options(data: Row) -> Row:
    """Values offered by the filters (whole scope, never filtered)."""
    coops = data["cooperatives"]
    crops: Dict[str, str] = {}
    for c in coops:
        for crop in c.get("crops") or []:
            crops.setdefault(crop.strip().lower(), crop.strip())
    regions = sorted({c["region"] for c in coops if c.get("region")}, key=str.lower)
    communes = sorted({(c["commune"], c.get("region")) for c in coops if c.get("commune")},
                      key=lambda x: x[0].lower())
    return {
        "cooperatives": [{"id": c["id"], "name": c["name"]} for c in coops],
        "crops": sorted(crops.values(), key=str.lower),
        "regions": regions,
        "communes": [{"name": n, "region": r} for n, r in communes],
        "statuses": ["considered", "banned"],
    }


def _point(row: Row) -> Optional[Row]:
    if row.get("lat") is None or row.get("lng") is None:
        return None
    return {"type": "Point", "coordinates": [row["lng"], row["lat"]]}


def _collection(rows: List[Row], geometry) -> Row:
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "id": r["id"],
        "geometry": geometry(r),
        "properties": {k: v for k, v in r.items() if k != "geometry"},
    } for r in rows]}


def member_dashboard(uid: str, role: Optional[str], filters: DashboardFilters) -> Row:
    """GeoJSON layers, indicators and filter options of the member's scope."""
    raw = _load(uid)
    data = {k: raw.get(k) or [] for k in ("cooperatives", "farmers", "parcels")}
    options = _options(data)
    if filters.active():
        data = _filter(data, filters)
    return {
        "scope": {"role": role, "filtered": filters.active()},
        "stats": _stats(data),
        "options": options,
        "cooperatives": _collection(data["cooperatives"], _point),
        "farmers": _collection(data["farmers"], _point),
        "parcels": _collection(data["parcels"], lambda r: r.get("geometry")),
    }
