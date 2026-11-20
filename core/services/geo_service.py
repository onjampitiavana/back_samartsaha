"""
Parcel geometry import / export.

Pure geometry work - no database access.
Import: KML / GeoPackage / zipped Shapefile -> GeoJSON features (WGS84) with
the area in hectares. Export: parcels as KML or a zipped Shapefile, both
written in pure Python (no GDAL writer) so they behave the same on Windows,
Linux and macOS.
"""

import json
import os
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import pandas as pd
import shapefile  # pyshp
from shapely import force_2d, make_valid
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.polygon import orient
from werkzeug.utils import secure_filename

import fiona
# Fiona doesn't enable the KML driver for reading by default.
fiona.drvsupport.supported_drivers["KML"] = "rwa"
import geopandas as gpd  # noqa: E402  (must come after the KML driver line above)

ALLOWED_PARCEL_EXTENSIONS = {"kml", "gpkg", "zip"}

# Attribute holding the parcel name in an imported file, first match wins
# (KML placemarks are read as "Name").
PARCEL_NAME_COLUMNS = ("nom", "Nom", "NOM", "name", "Name", "NAME")


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------
def _read_vector_file(path: str, ext: str) -> gpd.GeoDataFrame:
    """Read a vector file into a single GeoDataFrame.

    geopandas defaults to the `pyogrio` engine, which resolves .kml files to
    GDAL's optional 'LIBKML' driver. Most pip-installed GDAL builds don't
    ship libkml, so that raises `unsupported driver: 'LIBKML'`. Forcing the
    `fiona` engine with the plain 'KML' driver (enabled above) avoids that.
    A KML can also hold several folders as separate layers, so read every
    layer and stack them into one GeoDataFrame.
    """
    if ext != "kml":
        return gpd.read_file(path)

    layers = fiona.listlayers(path)
    frames = [
        gpd.read_file(path, engine="fiona", driver="KML", layer=layer)
        for layer in layers
    ]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return gpd.GeoDataFrame(geometry=[])
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)


def _as_parcel_geometry(geom) -> Optional[Any]:
    """The 2D, valid (Multi)Polygon a parcel can be stored as, or None.

    parcels.geom is a 2D column (geometry(Geometry, 4326)): KML always
    carries an altitude ("lon,lat,0"), and PostGIS refuses to store a 3D
    shape there ("Column has Z dimension but geometry does not"), so the
    altitude is dropped. Self-intersecting rings are repaired, and only the
    polygonal parts are kept (points / lines are not parcels)."""
    geom = force_2d(geom)
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom.geom_type == "GeometryCollection":
        polygons = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        parts = [p for g in polygons for p in (g.geoms if isinstance(g, MultiPolygon) else [g])]
        geom = parts[0] if len(parts) == 1 else MultiPolygon(parts) if parts else None
    if geom is None or geom.is_empty or not isinstance(geom, (Polygon, MultiPolygon)):
        return None
    return geom


_GEOMETRY_LABELS_FR = {
    "Point": "un point", "MultiPoint": "des points", "LineString": "une ligne",
    "MultiLineString": "des lignes", "LinearRing": "une ligne fermée",
}


def _clean_name(value: Any) -> Optional[str]:
    """Parcel name read from a file: trimmed text, None if empty / NaN."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = str(value).strip()
    return text[:120] or None


def extract_features_from_upload(file_storage, tmp_root: str) -> List[Dict[str, Any]]:
    """read_parcel_file() without the list of ignored shapes."""
    return read_parcel_file(file_storage, tmp_root)[0]


def read_parcel_file(file_storage, tmp_root: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read an uploaded .kml, .gpkg, or .zip (zipped Shapefile) and return a
    list of {"geometry": <geojson dict>, "area_ha": <float or None>,
    "nom": <name column of the file, or None>} — one
    entry per feature in the file, reprojected to WGS84 (EPSG:4326) for
    storage and to an appropriate UTM zone to compute area in hectares.
    `tmp_root` is the folder temporary files are written under.

    Returns (features, ignored): `ignored` describes, in French, the shapes
    that are not parcels (points, lines, empty shapes). Raises ValueError
    with a French message when the file holds no parcel at all."""
    filename = secure_filename(file_storage.filename or "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_PARCEL_EXTENSIONS:
        raise ValueError("Format de fichier non pris en charge. Envoyez un fichier .kml, "
                         ".gpkg ou un .zip contenant un Shapefile (.shp, .shx, .dbf, .prj).")

    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp_dir:
        saved_path = os.path.join(tmp_dir, filename)
        file_storage.save(saved_path)

        if ext == "zip":
            with zipfile.ZipFile(saved_path) as zf:
                zf.extractall(tmp_dir)
            shp_files = [f for f in os.listdir(tmp_dir) if f.lower().endswith(".shp")]
            if not shp_files:
                raise ValueError("Aucun fichier .shp trouvé dans l'archive .zip.")
            read_path = os.path.join(tmp_dir, shp_files[0])
        else:
            read_path = saved_path

        gdf = _read_vector_file(read_path, ext)
        if gdf.empty:
            raise ValueError("Le fichier ne contient aucune forme.")

        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=4326)
        gdf_4326 = gdf.to_crs(epsg=4326)

        try:
            utm_crs = gdf.estimate_utm_crs()
            areas_m2 = gdf.to_crs(utm_crs).geometry.area
        except Exception:
            areas_m2 = [None] * len(gdf)

        name_column = next((c for c in PARCEL_NAME_COLUMNS if c in gdf_4326.columns), None)
        names = gdf_4326[name_column] if name_column else [None] * len(gdf_4326)

        features, rejected = [], []
        for index, (geom, area_m2, name) in enumerate(zip(gdf_4326.geometry, areas_m2, names), 1):
            label = _clean_name(name) or f"forme n° {index}"
            if geom is None or geom.is_empty:
                rejected.append(f"« {label} » : forme vide")
                continue
            parcel_geom = _as_parcel_geometry(geom)
            if parcel_geom is None:
                kind = _GEOMETRY_LABELS_FR.get(geom.geom_type, geom.geom_type)
                rejected.append(f"« {label} » : {kind}, pas un polygone")
                continue
            features.append({
                "geometry": parcel_geom.__geo_interface__,
                "area_ha": round(area_m2 / 10000, 4) if area_m2 is not None else None,
                "nom": _clean_name(name),
            })

        if not features:
            detail = (" Formes ignorées : " + " ; ".join(rejected[:5])
                      + (" …" if len(rejected) > 5 else "") + ".") if rejected else ""
            raise ValueError("Le fichier ne contient aucun polygone de parcelle (contour fermé "
                             "d'au moins 3 points)." + detail)
        return features, rejected


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def parcels_geodataframe(rows: List[Dict[str, Any]]) -> Optional[gpd.GeoDataFrame]:
    """Build a GeoDataFrame from get_my_parcels() rows, with short column
    names so ESRI Shapefile doesn't truncate them. None if no geometry."""
    records, geoms = [], []
    for r in rows:
        if not r.get("geojson"):
            continue
        geoms.append(shape(json.loads(r["geojson"])))
        records.append({
            "nom": r.get("nom"),
            "coop": r.get("cooperative_name"),
            "farmer": r.get("farmer_name"),
            "area_ha": r.get("area_ha"),
            "created": r.get("created_at"),
        })
    if not records:
        return None
    return gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")


def _geometry_to_kml_geometry(geom) -> str:
    """Convert a shapely Polygon/MultiPolygon to raw KML geometry XML."""
    def ring_coords(ring):
        """KML coordinate string of one ring."""
        return " ".join(f"{x},{y},0" for x, y in ring.coords)

    def polygon_xml(poly):
        """KML <Polygon> of one shapely polygon, holes included."""
        outer = (f"<outerBoundaryIs><LinearRing><coordinates>"
                 f"{ring_coords(poly.exterior)}</coordinates></LinearRing></outerBoundaryIs>")
        inner = "".join(
            f"<innerBoundaryIs><LinearRing><coordinates>{ring_coords(r)}"
            f"</coordinates></LinearRing></innerBoundaryIs>"
            for r in poly.interiors
        )
        return f"<Polygon>{outer}{inner}</Polygon>"

    if geom.geom_type == "Polygon":
        return polygon_xml(geom)
    elif geom.geom_type == "MultiPolygon":
        return "<MultiGeometry>" + "".join(polygon_xml(p) for p in geom.geoms) + "</MultiGeometry>"
    else:
        raise ValueError("Type de géométrie non pris en charge pour l'export KML : "
                         f"{geom.geom_type}")


def build_kml(gdf: gpd.GeoDataFrame) -> bytes:
    """Hand-build KML XML instead of using GDAL/fiona's KML writer, which has
    a known bug on Windows (WinError 267) when writing .kml files. This is
    pure Python - no GDAL involved, so it's identical on Windows/Linux/Mac."""
    placemarks = []
    for _, row in gdf.iterrows():
        name = escape(str(row.get("nom") or row.get("farmer") or "Parcelle"))
        desc_parts = [f"{c}: {row[c]}" for c in gdf.columns if c != "geometry"]
        description = escape(" | ".join(str(d) for d in desc_parts))
        geom_xml = _geometry_to_kml_geometry(row.geometry)
        placemarks.append(
            f"<Placemark><name>{name}</name>"
            f"<description>{description}</description>{geom_xml}</Placemark>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        + "".join(placemarks) + "</Document></kml>"
    ).encode("utf-8")


def build_shapefile_zip(gdf: gpd.GeoDataFrame, out_dir: str,
                        base_name: str = "parcels") -> str:
    """Write a Shapefile using pyshp (pure Python, no GDAL) instead of
    geopandas.to_file(). geopandas/GDAL's ESRI Shapefile writer has a known
    Windows-specific bug (WinError 267) with certain geometry sets; pyshp
    avoids GDAL entirely, so it behaves identically on Windows/Linux/Mac."""
    shp_path = os.path.join(out_dir, base_name)
    with shapefile.Writer(shp_path, shapeType=shapefile.POLYGON) as w:
        w.field("coop", "C", size=80)
        w.field("farmer", "C", size=80)
        w.field("area_ha", "N", decimal=4)
        w.field("created", "C", size=30)

        for _, row in gdf.iterrows():
            geom = row.geometry
            polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
            parts = []
            for poly in polys:
                # Shapefile spec requires outer rings clockwise, holes counter-clockwise -
                # the opposite of GeoJSON/shapely's usual convention.
                oriented = orient(poly, sign=-1.0)
                parts.append(list(oriented.exterior.coords))
                for interior in oriented.interiors:
                    parts.append(list(interior.coords))
            w.poly(parts)
            w.record(row.get("coop"), row.get("farmer"), row.get("area_ha"),
                     str(row.get("created")))

    with open(shp_path + ".prj", "w") as f:
        f.write(
            'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
            'SPHEROID["WGS_1984",6378137,298.257223563]],'
            'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]'
        )

    zip_path = os.path.join(out_dir, f"{base_name}_shapefile.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            fn = base_name + ext
            zf.write(os.path.join(out_dir, fn), fn)
    return zip_path
