"""
KoboToolbox integration (Avenant n°1, lots A, B and C).

  connection   each admin stores his own personal API token (Paramètres),
               encrypted (crypto_service) in public.kobo_connections. It is
               only decrypted here, to call Kobo, and never sent back.
  import       a Kobo form ("asset") becomes a kobo_forms row and a table of
               the private schema kobo_data (one per form).
  sync         KoboToolbox REST v2 is only called on demand (or every
               KOBO_SYNC_MINUTES): just the submissions whose _id is above
               the last one imported, and just the question fields. Records
               already there are never duplicated nor overwritten (kobo_ingest).
  geometry     geopoint / geotrace / geoshape questions are detected from the
               form definition (or guessed from the answers: "uncertain");
               the admin can change the column and its nature. PostGIS
               builds the geometry (kobo_parse_geo, EPSG:4326).
  map          visible forms are a layer of the Carte menu for the members of
               the form's cooperative (and its owner).
"""

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from core import config
from core.models import Roles
from core.services import crypto_service
from core.services.clients import service_db
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

KOBO_SERVERS = {
    "https://kf.kobotoolbox.org": "KoboToolbox (serveur mondial)",
    "https://eu.kobotoolbox.org": "KoboToolbox (serveur Union européenne)",
}
PAGE_SIZE = 1000                    # submissions per Kobo request
ASSETS_CACHE_S = 300                # Kobo form list kept 5 min per member
TIMEOUT = httpx.Timeout(30, connect=10)

GEO_TYPES = {"geopoint": "point", "geotrace": "line", "geoshape": "polygon"}
GEO_KINDS = ("point", "line", "polygon")
# Question types that carry no answer.
_NO_DATA_TYPES = {"begin_group", "end_group", "begin_repeat", "end_repeat", "note",
                  "begin_score", "end_score", "begin_rank", "end_rank", "start", "end",
                  "today", "deviceid", "username", "simserial", "subscriberid", "phonenumber",
                  "audit"}
_GEO_VALUE_RE = re.compile(r"^\s*-?\d{1,2}(\.\d+)?\s+-?\d{1,3}(\.\d+)?(\s+[-\d.]+){0,2}\s*(;|$)")

_assets_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


class KoboError(ServiceError):
    """A KoboToolbox call failed, with a message for the user."""


# --------------------------------------------------------------------------
# Connection (Paramètres)
# --------------------------------------------------------------------------
def normalise_server(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url in KOBO_SERVERS:
        return url
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") \
            or parsed.query or parsed.username:
        raise ServiceError("Adresse du serveur Kobo invalide : utilisez une adresse https "
                           "(ex. https://kf.kobotoolbox.org).")
    host = parsed.hostname.lower()
    if host in ("localhost",) or re.fullmatch(r"[\d.]+|\[.*\]", host):
        raise ServiceError("Adresse du serveur Kobo invalide.")
    return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")


def _kobo_get(server: str, token: str, path: str, params: Optional[dict] = None) -> Any:
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            response = client.get(f"{server}{path}", params=params,
                                  headers={"Authorization": f"Token {token}",
                                           "Accept": "application/json"})
    except httpx.HTTPError as exc:
        log.warning("Kobo %s unreachable: %s", server, exc)
        raise KoboError("Le serveur KoboToolbox est injoignable. Réessayez plus tard.") from exc
    if response.status_code in (401, 403):
        raise KoboError("KoboToolbox a refusé le jeton d'accès : vérifiez-le dans Paramètres.")
    if response.status_code == 404:
        raise KoboError("Ce formulaire est introuvable sur KoboToolbox.")
    if response.status_code >= 400:
        log.warning("Kobo %s%s -> %s %s", server, path, response.status_code, response.text[:300])
        raise KoboError(f"KoboToolbox a répondu par une erreur ({response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise KoboError("Réponse illisible de KoboToolbox.") from exc


def connection_status(uid: str) -> Dict[str, Any]:
    rows = (service_db().table("kobo_connections")
            .select("server_url, token_hint, kobo_username, verified_at, updated_at")
            .eq("user_id", uid).limit(1).execute().data)
    row = rows[0] if rows else None
    return {"connected": bool(row), "connection": row, "servers": KOBO_SERVERS}


def save_connection(uid: str, server_url: str, token: str) -> Dict[str, Any]:
    server = normalise_server(server_url)
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", token):
        raise ServiceError("Le jeton Kobo doit être la clé de 40 caractères affichée dans "
                           "« Paramètres du compte > Sécurité > Clé API » de KoboToolbox.")
    me = _kobo_get(server, token, "/me/") or {}
    username = me.get("username") if isinstance(me, dict) else None
    if not username:
        raise KoboError("KoboToolbox n'a pas reconnu ce jeton.")
    now = datetime.now(timezone.utc).isoformat()
    service_db().table("kobo_connections").upsert({
        "user_id": uid, "server_url": server, "token_encrypted": crypto_service.encrypt(token),
        "token_hint": token[-4:], "kobo_username": username, "verified_at": now, "updated_at": now,
    }).execute()
    with _cache_lock:
        _assets_cache.pop(uid, None)
    return connection_status(uid)


def delete_connection(uid: str) -> None:
    service_db().table("kobo_connections").delete().eq("user_id", uid).execute()
    with _cache_lock:
        _assets_cache.pop(uid, None)


def _credentials(uid: str) -> Tuple[str, str]:
    rows = (service_db().table("kobo_connections").select("server_url, token_encrypted")
            .eq("user_id", uid).limit(1).execute().data)
    if not rows:
        raise KoboError("Connectez d'abord votre compte KoboToolbox dans Paramètres.")
    token = crypto_service.decrypt(rows[0]["token_encrypted"])
    if not token:
        raise KoboError("Le jeton Kobo enregistré n'est plus lisible : saisissez-le à nouveau "
                        "dans Paramètres.")
    return rows[0]["server_url"], token


# --------------------------------------------------------------------------
# Kobo forms (assets)
# --------------------------------------------------------------------------
def _label(question: Dict[str, Any]) -> str:
    label = question.get("label")
    if isinstance(label, list):
        label = next((l for l in label if l), None)
    return str(label or question.get("name") or question.get("$autoname") or "").strip()


def list_assets(uid: str, refresh: bool = False) -> List[Dict[str, Any]]:
    """The member's deployed Kobo survey forms (light: uid, name, count).
    Kept 5 minutes in memory: opening the page again costs no Kobo call."""
    with _cache_lock:
        hit = _assets_cache.get(uid)
    if hit and not refresh and time.monotonic() - hit[0] < ASSETS_CACHE_S:
        assets = hit[1]
    else:
        server, token = _credentials(uid)
        data = _kobo_get(server, token, "/api/v2/assets/",
                         {"format": "json", "q": "asset_type:survey", "limit": 300,
                          "metadata": "off"})
        assets = [{
            "uid": a.get("uid"),
            "name": a.get("name") or a.get("uid"),
            "submissions": a.get("deployment__submission_count") or 0,
            "deployed": bool(a.get("has_deployment") or a.get("deployment__active")),
            "modified": a.get("date_modified"),
        } for a in (data or {}).get("results", []) if a.get("uid")]
        with _cache_lock:
            _assets_cache[uid] = (time.monotonic(), assets)
    imported = {r["asset_uid"]: r["id"] for r in
                service_db().table("kobo_forms").select("id, asset_uid").eq("owner", uid)
                .execute().data or []}
    return [{**a, "imported_id": imported.get(a["uid"])} for a in assets]


def _columns_of(asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Answer columns from the Kobo form definition: the key is the one Kobo
    uses in submissions (group path included)."""
    columns, seen = [], set()
    repeat_depth = 0
    for q in ((asset.get("content") or {}).get("survey") or []):
        qtype = str(q.get("type") or "").split(" ")[0]
        # A repeat group's answers come as one list under the group's key.
        if qtype == "begin_repeat":
            if repeat_depth == 0:
                key = q.get("$xpath") or q.get("name") or q.get("$autoname")
                if key and key not in seen:
                    seen.add(key)
                    columns.append({"key": key, "label": _label(q) or key, "type": "repeat"})
            repeat_depth += 1
            continue
        if qtype == "end_repeat":
            repeat_depth = max(0, repeat_depth - 1)
            continue
        if repeat_depth or qtype in _NO_DATA_TYPES:
            continue
        key = q.get("$xpath") or q.get("name") or q.get("$autoname")
        if not key or key in seen:
            continue
        seen.add(key)
        columns.append({"key": key, "label": _label(q) or key, "type": qtype})
    return columns


def _detect_geo(columns: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], str]:
    geo = [c for c in columns if c["type"] in GEO_TYPES]
    if not geo:
        return None, None, "none"
    # Several geo questions: the most "parcel-like" is proposed, to confirm.
    order = {"geoshape": 0, "geotrace": 1, "geopoint": 2}
    best = sorted(geo, key=lambda c: order[c["type"]])[0]
    return best["key"], GEO_TYPES[best["type"]], "auto" if len(geo) == 1 else "uncertain"


def _guess_geo_from_data(records: List[Dict[str, Any]],
                         columns: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """A column whose answers look like Kobo coordinates ("lat lng alt acc")."""
    for c in columns:
        values = [r.get(c["key"]) for r in records[:30] if isinstance(r.get(c["key"]), str)]
        if values and all(_GEO_VALUE_RE.match(v) for v in values):
            vertices = max(v.count(";") + 1 for v in values)
            return c["key"], "point" if vertices == 1 else "polygon"
    return None, None


def _my_cooperative_ids(uid: str) -> List[str]:
    rows = service_db().rpc("fire_member_cooperatives", {"p_uid": uid}).execute().data or []
    return [r if isinstance(r, str) else r.get("fire_member_cooperatives") for r in rows]


def _form(uid: str, form_id: str) -> Dict[str, Any]:
    rows = (service_db().table("kobo_forms").select("*").eq("id", form_id).eq("owner", uid)
            .limit(1).execute().data)
    if not rows:
        raise ServiceError("Ce formulaire Kobo est introuvable.")
    return rows[0]


def import_form(uid: str, asset_uid: str, cooperative_id: Optional[str]) -> Dict[str, Any]:
    """Create the Smart Saha copy of a Kobo form, then import its submissions."""
    if not re.fullmatch(r"[A-Za-z0-9]{8,40}", asset_uid or ""):
        raise ServiceError("Formulaire Kobo invalide.")
    if cooperative_id and cooperative_id not in _my_cooperative_ids(uid):
        raise ServiceError("Cette coopérative est introuvable.")
    existing = (service_db().table("kobo_forms").select("id").eq("owner", uid)
                .eq("asset_uid", asset_uid).limit(1).execute().data)
    if existing:
        return sync_form(uid, existing[0]["id"])

    server, token = _credentials(uid)
    asset = _kobo_get(server, token, f"/api/v2/assets/{asset_uid}/", {"format": "json"})
    columns = _columns_of(asset)
    if not columns:
        raise KoboError("Ce formulaire Kobo n'a aucune question à importer.")
    geo_column, geo_kind, detection = _detect_geo(columns)
    form_id = str(uuid.uuid4())
    service_db().table("kobo_forms").insert({
        "id": form_id, "owner": uid, "cooperative_id": cooperative_id or None,
        "asset_uid": asset_uid, "name": (asset.get("name") or asset_uid)[:200],
        "columns": columns, "geo_column": geo_column, "geo_kind": geo_kind,
        "geo_detection": detection, "table_name": f"k_{form_id.replace('-', '')}",
    }).execute()
    service_db().rpc("kobo_create_table", {"p_form_id": form_id}).execute()
    return sync_form(uid, form_id)


@dataclass
class SyncResult:
    received: int = 0
    inserted: int = 0
    duplicates: int = 0


def sync_form(uid: Optional[str], form_id: str) -> Dict[str, Any]:
    """Download only the new submissions (Kobo _id above the last imported)
    and log the operation. uid None: scheduled run (form owner's token)."""
    db = service_db()
    form = _form(uid, form_id) if uid else (
        db.table("kobo_forms").select("*").eq("id", form_id).limit(1).execute().data or [None])[0]
    if not form:
        raise ServiceError("Ce formulaire Kobo est introuvable.")
    owner = form["owner"]
    log_row = db.table("kobo_import_logs").insert({
        "kobo_form_id": form_id, "triggered_by": uid}).execute().data[0]
    result = SyncResult()
    error = None
    try:
        server, token = _credentials(owner)
        keys = [c["key"] for c in form["columns"]]
        last = int(form.get("last_kobo_id") or 0)
        sample: List[Dict[str, Any]] = []
        while True:
            page = _kobo_get(server, token, f"/api/v2/assets/{form['asset_uid']}/data/", {
                "format": "json", "limit": PAGE_SIZE,
                "query": json.dumps({"_id": {"$gt": last}}),
                "sort": json.dumps({"_id": 1}),
                # Only what Smart Saha keeps: lighter answers from Kobo.
                "fields": json.dumps(["_id", "_uuid", "_submission_time", *keys]),
            })
            results = (page or {}).get("results") or []
            if not results:
                break
            rows = []
            for r in results:
                data = {k: r[k] for k in keys if k in r and r[k] not in (None, "")}
                rows.append({"kobo_id": r.get("_id"), "uuid": r.get("_uuid"),
                             "submitted_at": r.get("_submission_time"), "data": data})
                if len(sample) < 30:
                    sample.append(data)
            stored = db.rpc("kobo_ingest", {"p_form_id": form_id, "p_rows": rows}).execute().data
            result.received += len(rows)
            result.inserted += int(stored.get("inserted") or 0)
            result.duplicates += int(stored.get("duplicates") or 0)
            new_last = int(stored.get("last_kobo_id") or last)
            if new_last <= last or len(results) < PAGE_SIZE:
                break
            last = new_last
        # No geo question in the definition: try the answers ("uncertain").
        if form["geo_detection"] == "none" and sample:
            column, kind = _guess_geo_from_data(sample, form["columns"])
            if column:
                db.table("kobo_forms").update({"geo_column": column, "geo_kind": kind,
                                               "geo_detection": "uncertain"}).eq("id", form_id).execute()
                db.rpc("kobo_recompute_geoms", {"p_form_id": form_id}).execute()
    except ServiceError as exc:
        error = str(exc)
    except Exception as exc:
        log.exception("Kobo sync of %s failed", form_id)
        error = "Erreur inattendue pendant la synchronisation."
    db.table("kobo_import_logs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if error else "ok", "received": result.received,
        "inserted": result.inserted, "duplicates": result.duplicates, "error": error,
    }).eq("id", log_row["id"]).execute()
    if error and uid:
        raise ServiceError(f"Synchronisation interrompue : {error}"
                           + (f" ({result.inserted} soumission(s) importée(s) avant l'erreur.)"
                              if result.inserted else ""))
    return {"form_id": form_id, "received": result.received, "inserted": result.inserted,
            "duplicates": result.duplicates}


def sync_all() -> int:
    """Scheduled run: every imported form (only new submissions)."""
    n = 0
    for row in service_db().table("kobo_forms").select("id").execute().data or []:
        try:
            sync_form(None, row["id"])
            n += 1
        except Exception:
            log.exception("Scheduled Kobo sync of %s failed", row["id"])
    return n


# --------------------------------------------------------------------------
# Imported forms and their records (Formulaires menu)
# --------------------------------------------------------------------------
_FORM_COLUMNS = ("id, asset_uid, name, cooperative_id, columns, geo_column, geo_kind, "
                 "geo_detection, record_count, last_sync_at, created_at, updated_at")


def list_forms(uid: str) -> Dict[str, Any]:
    db = service_db()
    forms = (db.table("kobo_forms").select(_FORM_COLUMNS).eq("owner", uid)
             .order("created_at", desc=True).execute().data or [])
    coop_ids = _my_cooperative_ids(uid)
    coops = (db.table("cooperative").select("uuid, name").in_("uuid", coop_ids).order("name")
             .execute().data if coop_ids else [])
    for f in forms:
        f["column_count"] = len(f.pop("columns") or [])
    return {"forms": forms, "cooperatives": coops}


def form_detail(uid: str, form_id: str) -> Dict[str, Any]:
    form = _form(uid, form_id)
    form.pop("owner", None)
    form.pop("table_name", None)
    logs = (service_db().table("kobo_import_logs")
            .select("id, started_at, finished_at, status, received, inserted, duplicates, error")
            .eq("kobo_form_id", form_id).order("id", desc=True).limit(30).execute().data or [])
    return {"form": form, "logs": logs, "cooperatives": list_forms(uid)["cooperatives"]}


def update_form(uid: str, form_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    form = _form(uid, form_id)
    changes: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not 2 <= len(name) <= 200:
            raise ServiceError("Le nom doit faire entre 2 et 200 caractères.")
        changes["name"] = name
    if "cooperative_id" in payload:
        coop = payload.get("cooperative_id") or None
        if coop and coop not in _my_cooperative_ids(uid):
            raise ServiceError("Cette coopérative est introuvable.")
        changes["cooperative_id"] = coop
    geo_changed = False
    if "geo_column" in payload or "geo_kind" in payload:
        column = payload.get("geo_column") or None
        kind = payload.get("geo_kind") or None
        if column and column not in {c["key"] for c in form["columns"]}:
            raise ServiceError("Cette colonne n'existe pas dans le formulaire.")
        if column and kind not in GEO_KINDS:
            raise ServiceError("Choisissez la nature de la géométrie : point, ligne ou polygone.")
        changes.update({"geo_column": column, "geo_kind": kind if column else None,
                        "geo_detection": "manual" if column else "none"})
        geo_changed = (column, kind) != (form.get("geo_column"), form.get("geo_kind"))
    service_db().table("kobo_forms").update(changes).eq("id", form_id).execute()
    located = None
    if geo_changed:
        located = service_db().rpc("kobo_recompute_geoms", {"p_form_id": form_id}).execute().data
    return {"located": located}


def delete_form(uid: str, form_id: str) -> str:
    form = _form(uid, form_id)
    service_db().rpc("kobo_drop_form", {"p_form_id": form_id}).execute()
    return form["name"]


def records(uid: str, form_id: str, offset: int, search: Optional[str]) -> Dict[str, Any]:
    _form(uid, form_id)
    return service_db().rpc("kobo_records", {"p_form_id": form_id, "p_limit": 50,
                                             "p_offset": max(offset, 0),
                                             "p_search": (search or "")[:100] or None}
                            ).execute().data or {"total": 0, "records": []}


def _clean_record(form: Dict[str, Any], data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ServiceError("Enregistrement invalide.")
    keys = {c["key"] for c in form["columns"]}
    clean = {}
    for key, value in data.items():
        if key not in keys or value in (None, ""):
            continue
        if isinstance(value, (dict, list)):
            if len(json.dumps(value)) > 20000:
                raise ServiceError("Une valeur est trop longue.")
        elif isinstance(value, str):
            if len(value) > 10000:
                raise ServiceError("Une valeur est trop longue.")
            value = value.strip()
        elif not isinstance(value, (int, float, bool)):
            raise ServiceError("Une valeur n'est pas lisible.")
        clean[key] = value
    if not clean:
        raise ServiceError("Renseignez au moins une valeur.")
    return clean


def save_record(uid: str, form_id: str, record_id: Optional[str], data: Any) -> str:
    form = _form(uid, form_id)
    try:
        return service_db().rpc("kobo_record_save", {
            "p_form_id": form_id, "p_id": record_id,
            "p_data": _clean_record(form, data)}).execute().data
    except ServiceError:
        raise
    except Exception as exc:
        if "Record not found" in str(exc):
            raise ServiceError("Cet enregistrement est introuvable.") from exc
        log.warning("Kobo record save failed: %s", exc)
        raise ServiceError("L'enregistrement n'a pas pu être sauvegardé.") from exc


def delete_record(uid: str, form_id: str, record_id: str) -> bool:
    _form(uid, form_id)
    return bool(service_db().rpc("kobo_record_delete", {"p_form_id": form_id, "p_id": record_id})
                .execute().data)


# --------------------------------------------------------------------------
# Map layers (Carte menu, every role)
# --------------------------------------------------------------------------
def _visible_forms(uid: str, role: Optional[str]) -> List[Dict[str, Any]]:
    db = service_db()
    query = db.table("kobo_forms").select(
        "id, owner, name, cooperative_id, columns, geo_kind, record_count, updated_at"
    ).not_.is_("geo_column", "null")
    rows = query.execute().data or []
    if role == Roles.SUPERADMIN:
        return rows
    coops = set(_my_cooperative_ids(uid))
    return [r for r in rows if r["owner"] == uid or (r.get("cooperative_id") in coops)]


def _layer_version(row: Dict[str, Any]) -> str:
    raw = f"{row['id']}|{row.get('updated_at')}|{row.get('record_count')}|{row.get('geo_kind')}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def layers(uid: str, role: Optional[str]) -> List[Dict[str, Any]]:
    return [{"id": r["id"], "name": r["name"], "geo_kind": r["geo_kind"],
             "count": r["record_count"], "v": _layer_version(r)}
            for r in _visible_forms(uid, role)]


def _visible_form(uid: str, role: Optional[str], form_id: str) -> Dict[str, Any]:
    form = next((r for r in _visible_forms(uid, role) if r["id"] == form_id), None)
    if not form:
        raise ServiceError("Cette couche est introuvable.")
    return form


def layer(uid: str, role: Optional[str], form_id: str) -> Tuple[str, List[Any]]:
    form = _visible_form(uid, role, form_id)
    data = service_db().rpc("kobo_layer", {"p_form_id": form_id}).execute().data or {}
    return _layer_version(form), data.get("f") or []


def popup(uid: str, role: Optional[str], form_id: str, record_id: str) -> Dict[str, Any]:
    form = _visible_form(uid, role, form_id)
    rec = service_db().rpc("kobo_record", {"p_form_id": form_id, "p_id": record_id}).execute().data
    if not rec:
        raise ServiceError("Cet enregistrement est introuvable.")
    data = rec.get("data") or {}
    fields = []
    for c in form["columns"]:
        value = data.get(c["key"])
        if value in (None, "") or c["type"] in GEO_TYPES:
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)[:300]
        fields.append([c["label"], str(value)[:300]])
        if len(fields) >= 12:
            break
    return {"form": form["name"], "submitted_at": rec.get("submitted_at"),
            "source": rec.get("source"), "fields": fields}


# --------------------------------------------------------------------------
# Background re-synchronisation (optional)
# --------------------------------------------------------------------------
_scheduler_started = False


def start_scheduler() -> None:
    global _scheduler_started
    if config.KOBO_SYNC_MINUTES <= 0 or _scheduler_started or not config.SUPABASE_SERVICE_ROLE_KEY:
        return
    _scheduler_started = True

    def loop() -> None:
        while True:
            time.sleep(max(15, config.KOBO_SYNC_MINUTES) * 60)
            try:
                sync_all()
            except Exception:
                log.exception("Scheduled Kobo sync failed")

    threading.Thread(target=loop, name="kobo-sync", daemon=True).start()
