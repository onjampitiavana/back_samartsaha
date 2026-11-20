"""
Fire hotspots (NASA FIRMS) and proximity alerts - feature 4.3.

Refresh (server only, migrations/20261118_fire_alerts.sql):

  refresh()   claims the next fetch in Supabase (fire_claim_fetch: at most one
              per FIRE_REFRESH_MINUTES, whatever the number of workers),
              downloads the hotspots of the parcels' bounding box from FIRMS
              with MAP_KEY, stores them (fire_ingest: duplicates ignored,
              alerts created by PostGIS within FIRE_ALERT_RADIUS_KM, one per
              hotspot and parcel) and e-mails the new alerts.

Reading, on behalf of the signed-in member (cooperatives he may see):

  member_map()      hotspots of a period + parcels at risk, compact arrays
  member_parcels()  simplified parcel outlines (cached by version)
  member_alerts()   alert log;  member_unread() / mark_read(): the badge

Users never trigger a FIRMS call: they only read the Supabase cache.
"""

import csv
import hashlib
import io
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from core import config
from core.services import mail_service
from core.services.clients import service_db
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

FIRMS_AREA_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}"
FIRMS_MAX_DAYS = 5          # largest DAY_RANGE the area API accepts
HISTORY_DAYS = 30           # longest period the maps offer
AREA_MARGIN_DEG = 0.1       # ~11 km around the parcels (covers the 5 km buffers)
INGEST_CHUNK = 2000

PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}

_MISSING_MIGRATION = ("La carte des feux n'est pas encore installée sur le serveur "
                      "(migration 20261118_fire_alerts.sql).")


@dataclass
class RefreshResult:
    """What one refresh did (or why it was skipped)."""
    skipped: Optional[str] = None
    received: int = 0
    inserted: int = 0
    alerts: int = 0
    emailed: int = 0


# --------------------------------------------------------------------------
# FIRMS
# --------------------------------------------------------------------------
def _confidence(raw: str) -> Optional[str]:
    """VIIRS gives l / n / h; MODIS a percentage, mapped to the same scale."""
    raw = (raw or "").strip().lower()
    if raw in ("l", "low"):
        return "l"
    if raw in ("n", "nominal"):
        return "n"
    if raw in ("h", "high"):
        return "h"
    try:
        value = float(raw)
    except ValueError:
        return None
    return "l" if value < 30 else "n" if value < 80 else "h"


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_firms_csv(text: str, source: str) -> List[Dict[str, Any]]:
    """FIRMS CSV -> the rows fire_ingest() expects (only the useful fields).
    Raises ValueError when the answer is not a FIRMS CSV (bad key, quota)."""
    text = text.lstrip("﻿")
    first_line = text.split("\n", 1)[0]
    if "latitude" not in first_line:
        raise ValueError(f"FIRMS {source}: {first_line.strip()[:200] or 'réponse vide'}")
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        lat, lng = _float(rec.get("latitude")), _float(rec.get("longitude"))
        day, hhmm = (rec.get("acq_date") or "").strip(), (rec.get("acq_time") or "").strip()
        if lat is None or lng is None or not day or not hhmm.isdigit():
            continue
        hhmm = hhmm.zfill(4)
        rows.append({
            "source": source,
            "lat": round(lat, 5),
            "lng": round(lng, 5),
            "t": f"{day}T{hhmm[:2]}:{hhmm[2:]}:00Z",
            "conf": _confidence(rec.get("confidence", "")),
            "frp": _float(rec.get("frp")),
            "bright": _float(rec.get("bright_ti4") or rec.get("brightness")),
            "dn": (rec.get("daynight") or "")[:1] or None,
        })
    return rows


def _bbox_text(extent: Dict[str, float]) -> str:
    return ",".join(f"{extent[k]:.4f}" for k in ("west", "south", "east", "north"))


def _requests(last_ok: Optional[datetime]) -> List[tuple]:
    """(day_range, start date or None) to ask FIRMS for: everything since the
    last successful fetch (at most 5 days, the most recent), or the whole
    30-day history on the very first run (in 5-day windows)."""
    if last_ok is None:
        today = datetime.now(timezone.utc).date()
        # windows ending today: [-29..-25], ..., [-4..0]
        return [(FIRMS_MAX_DAYS, today - timedelta(days=start))
                for start in range(HISTORY_DAYS - 1, FIRMS_MAX_DAYS - 2, -FIRMS_MAX_DAYS)]
    hours = (datetime.now(timezone.utc) - last_ok).total_seconds() / 3600
    return [(min(FIRMS_MAX_DAYS, max(1, math.ceil(hours / 24) + 1)), None)]


def _fetch_firms(client: httpx.Client, source: str, bbox: str, days: int,
                 start: Optional[date]) -> List[Dict[str, Any]]:
    url = FIRMS_AREA_URL.format(key=config.MAP_KEY, source=source, bbox=bbox, days=days)
    if start is not None:
        url += f"/{start.isoformat()}"
    response = client.get(url)
    response.raise_for_status()
    return parse_firms_csv(response.text, source)


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def refresh(force: bool = False) -> RefreshResult:
    """Fetch FIRMS if the cache is due (or `force`), store, alert, e-mail.
    Safe to call from several workers / threads: Supabase hands the fetch to
    one of them only."""
    if not config.MAP_KEY:
        return RefreshResult(skipped="MAP_KEY n'est pas défini dans .env")
    db = service_db()
    claim = db.rpc("fire_claim_fetch", {"p_interval_minutes": config.FIRE_REFRESH_MINUTES,
                                        "p_force": force}).execute().data
    if not claim:
        return RefreshResult(skipped="cache à jour")

    fetch_id = claim["id"]
    result = RefreshResult()
    bbox, plan = None, []
    try:
        extent = db.rpc("fire_parcels_extent", {"p_margin_deg": AREA_MARGIN_DEG}).execute().data
        if not extent:
            _finish(db, fetch_id, "ok", None, 0, result, None)
            return RefreshResult(skipped="aucune parcelle enregistrée")
        bbox = _bbox_text(extent)
        plan = _requests(_parse_ts(claim.get("last_ok")))

        rows: List[Dict[str, Any]] = []
        errors = []
        with httpx.Client(timeout=60) as client:
            for source in config.FIRE_SOURCES:
                for days, start in plan:
                    try:
                        rows.extend(_fetch_firms(client, source, bbox, days, start))
                    except Exception as exc:          # one source down: keep the others
                        errors.append(f"{source}: {exc}")
                        log.warning("FIRMS %s (%s days from %s) failed: %s",
                                    source, days, start or "today", exc)
        if errors and not rows and len(errors) == len(config.FIRE_SOURCES) * len(plan):
            raise RuntimeError("; ".join(errors)[:500])

        result.received = len(rows)
        radius_m = int(config.FIRE_ALERT_RADIUS_KM * 1000)
        chunks = [rows[i:i + INGEST_CHUNK] for i in range(0, len(rows), INGEST_CHUNK)] or [[]]
        for chunk in chunks:
            stored = db.rpc("fire_ingest", {"p_rows": chunk, "p_radius_m": radius_m}).execute().data
            result.inserted += int(stored.get("inserted") or 0)
            result.alerts += int(stored.get("alerts") or 0)
        _finish(db, fetch_id, "ok", bbox, max(d for d, _ in plan), result,
                "; ".join(errors) or None)
    except Exception as exc:
        log.exception("FIRMS refresh failed")
        _finish(db, fetch_id, "failed", bbox, max((d for d, _ in plan), default=None),
                result, str(exc))
        raise

    try:
        result.emailed = send_pending_emails()
    except Exception:
        log.exception("Fire alert e-mails failed (they will be retried)")
    log.info("FIRMS refresh: %s rows, %s new hotspots, %s alerts, %s e-mails",
             result.received, result.inserted, result.alerts, result.emailed)
    return result


def _finish(db, fetch_id, status, bbox, day_range, result: RefreshResult, error) -> None:
    try:
        db.rpc("fire_finish_fetch", {
            "p_id": fetch_id, "p_status": status, "p_bbox": bbox,
            "p_sources": ",".join(config.FIRE_SOURCES), "p_day_range": day_range,
            "p_received": result.received, "p_inserted": result.inserted,
            "p_alerts": result.alerts, "p_error": error,
        }).execute()
    except Exception:
        log.exception("Could not close fire_fetch_log row %s", fetch_id)


# --------------------------------------------------------------------------
# E-mails: one digest per recipient and run
# --------------------------------------------------------------------------
_CONFIDENCE_FR = {"l": "faible", "n": "nominale", "h": "élevée"}


def _alert_line(a: Dict[str, Any]) -> str:
    when = _parse_ts(a.get("acq_at"))
    when_txt = when.strftime("%d/%m/%Y %H:%M UTC") if when else "?"
    km = (a.get("distance_m") or 0) / 1000
    return (f"- Parcelle « {a.get('nom') or 'sans nom'} » ({a.get('farmer_name') or 'producteur ?'}) : "
            f"foyer à {km:.1f} km, détecté le {when_txt}, confiance "
            f"{_CONFIDENCE_FR.get(a.get('confidence'), 'inconnue')} "
            f"({a.get('latitude'):.4f}, {a.get('longitude'):.4f})")


def send_pending_emails() -> int:
    """E-mail the alerts not sent yet; returns the number of e-mails sent.
    Alerts are marked sent only once every recipient got them (or has no
    address), so a mail server outage is retried at the next refresh."""
    if not mail_service.is_configured():
        return 0
    db = service_db()
    pending = db.rpc("fire_pending_email_alerts", {"p_limit": 500}).execute().data or {}
    alerts = pending.get("alerts") or []
    if not alerts:
        return 0
    by_coop: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in alerts:
        by_coop[a["cooperative_id"]].append(a)
    per_recipient: Dict[str, Dict[str, Any]] = {}
    for r in pending.get("recipients") or []:
        entry = per_recipient.setdefault(r["email"].lower(), {"name": r.get("full_name"),
                                                              "coops": set()})
        entry["coops"].add(r["cooperative_id"])

    failed_coops = set()
    sent = 0
    for email, entry in per_recipient.items():
        mine = [a for coop in entry["coops"] for a in by_coop.get(coop, [])]
        if not mine:
            continue
        lines = []
        for coop in sorted(entry["coops"]):
            coop_alerts = by_coop.get(coop, [])
            if coop_alerts:
                lines.append(f"\nCoopérative {coop_alerts[0].get('cooperative_name')} :")
                lines.extend(_alert_line(a) for a in coop_alerts)
        n = len(mine)
        body = (f"Bonjour {entry['name'] or ''},\n\n"
                f"Smart Saha a détecté {n} foyer{'s' if n > 1 else ''} de chaleur (NASA FIRMS) "
                f"à moins de {config.FIRE_ALERT_RADIUS_KM:g} km de parcelles que vous suivez :\n"
                + "\n".join(lines)
                + "\n\nConsultez la carte et le journal des alertes dans Smart Saha (menu « Carte »)."
                  "\nCes détections satellitaires sont à confirmer sur le terrain.\n")
        try:
            mail_service.send(email, f"Smart Saha - alerte feu : {n} parcelle{'s' if n > 1 else ''} "
                                     "concernée" + ("s" if n > 1 else ""), body)
            sent += 1
        except mail_service.RecipientRefused:
            log.info("Fire alert e-mail refused for %s", email)
        except Exception:
            log.exception("Fire alert e-mail to %s failed", email)
            failed_coops.update(entry["coops"])

    done = [a["id"] for a in alerts if a["cooperative_id"] not in failed_coops]
    if done:
        db.rpc("fire_mark_emailed", {"p_ids": done}).execute()
    return sent


# --------------------------------------------------------------------------
# Reading for the signed-in member
# --------------------------------------------------------------------------
def _member_rpc(name: str, params: Dict[str, Any]) -> Any:
    try:
        return service_db().rpc(name, params).execute().data
    except ServiceError:
        raise
    except Exception as exc:
        text = str(exc)
        if "PGRST202" in text or "Could not find the function" in text:
            log.error("%s() is missing: run migrations/20261118_fire_alerts.sql", name)
            raise ServiceError(_MISSING_MIGRATION) from exc
        log.warning("%s failed: %s", name, exc)
        raise ServiceError("Les données des feux sont momentanément indisponibles. "
                           "Réessayez dans un instant.") from exc


def next_refresh(last_fetch: Optional[str]) -> Optional[str]:
    last = _parse_ts(last_fetch)
    if last is None:
        return None
    return (last + timedelta(minutes=config.FIRE_REFRESH_MINUTES)).isoformat()


def member_map(uid: str, period: str) -> Dict[str, Any]:
    """Hotspots of `period` (24h / 7d / 30d) around the member's parcels."""
    since = datetime.now(timezone.utc) - PERIODS.get(period, PERIODS["24h"])
    data = _member_rpc("fire_member_map", {"p_uid": uid, "p_since": since.isoformat(),
                                           "p_margin_deg": AREA_MARGIN_DEG}) or {}
    data["next_refresh"] = next_refresh(data.get("last_fetch"))
    data["alert_radius_km"] = config.FIRE_ALERT_RADIUS_KM
    return data


def member_parcels(uid: str) -> Dict[str, Any]:
    return _member_rpc("fire_member_parcels", {"p_uid": uid, "p_tolerance": 0.00001}) or {}


def member_alerts(uid: str, limit: int = 50, before_id: Optional[int] = None) -> List[Dict]:
    return _member_rpc("fire_member_alerts", {"p_uid": uid, "p_limit": limit,
                                              "p_before_id": before_id}) or []


def member_unread(uid: str) -> Dict[str, Any]:
    return _member_rpc("fire_member_unread", {"p_uid": uid}) or {"count": 0, "latest": []}


def mark_read(uid: str, last_id: int) -> int:
    return int(_member_rpc("fire_mark_read", {"p_uid": uid, "p_last_id": last_id}) or 0)


def parcels_version(geometries: Dict[str, Any]) -> str:
    """Same digest as fire_member_map's parcels_version (md5 of sorted uuids)."""
    return hashlib.md5(",".join(sorted(geometries)).encode()).hexdigest()[:16]

