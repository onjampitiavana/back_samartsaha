"""Administrative boundaries: régions and communes (admin_regions /
admin_communes, migrations/20261125_admin_boundaries.sql).

  list_regions() / list_communes()  the cooperative form's Région / Commune
                                    lists, from the boundaries tables.
  detect_for_cooperative()          région / commune containing the centre of
                                    a cooperative's parcels.
  import_boundaries()               fills the tables from a boundaries file
                                    (flask --app run geo-import).

Until the boundaries are imported, the lists fall back to the 23 régions of
Madagascar and to the communes already recorded on the member's
cooperatives, so the form keeps working.
"""

import json
import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from core.services.clients import service_db, user_db
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

# The 23 régions of Madagascar (since 2021: Vatovavy and Fitovinany).
FALLBACK_REGIONS = [
    "Alaotra-Mangoro", "Amoron'i Mania", "Analamanga", "Analanjirofo", "Androy",
    "Anosy", "Atsimo-Andrefana", "Atsimo-Atsinanana", "Atsinanana", "Betsiboka",
    "Boeny", "Bongolava", "Diana", "Fitovinany", "Haute Matsiatra", "Ihorombe",
    "Itasy", "Melaky", "Menabe", "Sava", "Sofia", "Vakinankaratra", "Vatovavy",
]

# Reference data hardly ever changes: read once per process (10 min).
_CACHE_TTL_S = 600
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _cached(key: str, load):
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[1] < _CACHE_TTL_S:
        return hit[0]
    value = load()
    with _cache_lock:
        _cache[key] = (value, time.monotonic())
    return value


def _forget() -> None:
    with _cache_lock:
        _cache.clear()


def _reference_regions() -> List[Dict[str, str]]:
    """[{code, name}] from admin_regions ([] if the table is missing / empty)."""
    def load():
        try:
            rows = (service_db().table("admin_regions").select("code, name")
                    .order("name").execute().data) or []
        except Exception as exc:
            log.info("admin_regions unavailable (run migrations/20261125_admin_boundaries.sql "
                     "then flask geo-import): %s", exc)
            return []
        return [{"code": r["code"], "name": r["name"]} for r in rows]
    return _cached("regions", load)


def _reference_communes() -> List[Dict[str, Optional[str]]]:
    """[{name, region, district}] from admin_communes ([] if unavailable)."""
    def load():
        regions = {r["code"]: r["name"] for r in _reference_regions()}
        out: List[Dict[str, Optional[str]]] = []
        try:
            db = service_db()
            start, page = 0, 1000                    # PostgREST returns 1000 rows at most
            while True:
                rows = (db.table("admin_communes").select("name, region_code, district")
                        .order("name").range(start, start + page - 1).execute().data) or []
                out += [{"name": r["name"], "region": regions.get(r.get("region_code")),
                         "district": r.get("district")} for r in rows]
                if len(rows) < page:
                    break
                start += page
        except Exception as exc:
            log.info("admin_communes unavailable: %s", exc)
            return []
        return out
    return _cached("communes", load)


def _recorded_places() -> List[Dict[str, Optional[str]]]:
    """Région / commune already typed on the member's cooperatives."""
    try:
        rows = user_db().table("cooperative").select("region, commune").execute().data or []
    except Exception:
        return []
    return [{"region": (r.get("region") or "").strip() or None,
             "commune": (r.get("commune") or "").strip() or None} for r in rows]


def _unique(values: Iterable[Optional[str]]) -> List[str]:
    seen: Dict[str, str] = {}
    for v in values:
        if v and v.strip():
            seen.setdefault(v.strip().lower(), v.strip())
    return sorted(seen.values(), key=str.lower)


def list_regions() -> Dict[str, Any]:
    """{regions: [...], source: 'boundaries' | 'fallback'}."""
    reference = [r["name"] for r in _reference_regions()]
    if reference:
        return {"regions": _unique(reference), "source": "boundaries"}
    recorded = [p["region"] for p in _recorded_places()]
    return {"regions": _unique([*FALLBACK_REGIONS, *recorded]), "source": "fallback"}


def list_communes(region: Optional[str] = None) -> Dict[str, Any]:
    """{communes: [{name, region, district}], source}, limited to `region`."""
    wanted = (region or "").strip().lower()
    reference = _reference_communes()
    if reference:
        rows = [c for c in reference if not wanted or (c["region"] or "").lower() == wanted]
        return {"communes": rows, "source": "boundaries"}
    seen: Dict[str, Dict[str, Optional[str]]] = {}
    for p in _recorded_places():
        if p["commune"] and (not wanted or (p["region"] or "").lower() == wanted):
            seen.setdefault(p["commune"].lower(), {"name": p["commune"], "region": p["region"],
                                                   "district": None})
    return {"communes": sorted(seen.values(), key=lambda c: c["name"].lower()),
            "source": "fallback"}


def detect_for_cooperative(cooperative_uuid: str) -> Dict[str, Any]:
    """{region, commune, parcels, lat, lng} from the centre of the
    cooperative's parcels (the member's own: Row Level Security)."""
    try:
        result = user_db().rpc("cooperative_detect_location",
                               {"p_cooperative": cooperative_uuid}).execute().data
    except Exception as exc:
        if "PGRST202" in str(exc) or "cooperative_detect_location" in str(exc):
            raise ServiceError("La détection n'est pas encore installée : exécutez "
                               "migrations/20261125_admin_boundaries.sql.") from exc
        log.warning("cooperative_detect_location failed: %s", exc)
        raise ServiceError("La localisation n'a pas pu être détectée. Veuillez réessayer.") from exc
    result = result or {}
    if not result.get("parcels"):
        raise ServiceError("Cette coopérative n'a encore aucune parcelle : sa position ne peut "
                           "pas être détectée. Choisissez la région et la commune.")
    if not result.get("region") and not result.get("commune"):
        raise ServiceError("Les parcelles sont en dehors des limites administratives importées "
                           "(ou celles-ci ne sont pas encore importées : flask geo-import).")
    return result


# --------------------------------------------------------------------------
# Import (CLI)
# --------------------------------------------------------------------------
def _pick(columns: Iterable[str], wanted: Optional[str], guesses: Iterable[str]) -> Optional[str]:
    cols = {c.lower(): c for c in columns}
    if wanted:
        if wanted.lower() not in cols:
            raise ValueError(f"Colonne « {wanted} » absente. Colonnes : {', '.join(columns)}")
        return cols[wanted.lower()]
    return next((cols[g] for g in guesses if g in cols), None)


def import_boundaries(kind: str, path: str, name_field: Optional[str] = None,
                      code_field: Optional[str] = None, region_field: Optional[str] = None,
                      district_field: Optional[str] = None, batch: int = 50) -> int:
    """Upsert the régions or communes of a boundaries file (Shapefile, GeoJSON,
    GeoPackage...). Field names default to the HDX COD-AB ones (ADM1_EN,
    ADM1_PCODE, ADM3_EN, ADM3_PCODE...). Returns the number of rows written."""
    import geopandas as gpd

    if kind not in ("region", "commune"):
        raise ValueError("kind : region ou commune")
    level = "1" if kind == "region" else "3"
    frame = gpd.read_file(path)
    if frame.crs is not None and frame.crs.to_epsg() != 4326:
        frame = frame.to_crs(4326)
    cols = [c for c in frame.columns if c != "geometry"]
    name_col = _pick(cols, name_field, [f"adm{level}_fr", f"adm{level}_en", "name", "nom"])
    code_col = _pick(cols, code_field, [f"adm{level}_pcode", "code", "pcode"])
    region_col = (_pick(cols, region_field, ["adm1_pcode"]) if kind == "commune" else None)
    district_col = (_pick(cols, district_field, ["adm2_fr", "adm2_en"]) if kind == "commune" else None)
    if not name_col or not code_col:
        raise ValueError(f"Précisez --name-field et --code-field. Colonnes : {', '.join(cols)}")

    rows = []
    for _, rec in frame.iterrows():
        if rec.geometry is None or rec.geometry.is_empty:
            continue
        row = {"code": str(rec[code_col]), "name": str(rec[name_col]).strip(),
               "geometry": json.dumps(rec.geometry.__geo_interface__)}
        if region_col:
            row["region_code"] = str(rec[region_col])
        if district_col:
            row["district"] = str(rec[district_col]).strip()
        rows.append(row)

    db = service_db()
    written = 0
    for i in range(0, len(rows), batch):
        written += int(db.rpc("admin_geo_upsert", {"p_kind": kind, "p_rows": rows[i:i + batch]})
                       .execute().data or 0)
    _forget()
    return written
