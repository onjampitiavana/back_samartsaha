

from flask import g, request

from core.models import Roles
from core.routes.api_bp import _fail, _ok, api_bp, api_login_required, api_role_required
from core.services import fire_service


@api_bp.get("/fires")
@api_login_required
def fires():
    period = request.args.get("period", "24h")
    if period not in fire_service.PERIODS:
        return _fail("Période inconnue (24h, 7d ou 30d).", 400)
    response, status = _ok(period=period, **fire_service.member_map(g.uid, period))
    # Hotspots change at most once per refresh: a short private cache spares
    # repeated downloads when switching filters back and forth.
    response.headers["Cache-Control"] = "private, max-age=300"
    return response, status


@api_bp.get("/fires/parcels")
@api_login_required
def fire_parcels():
    parcels = fire_service.member_parcels(g.uid)
    version = fire_service.parcels_version(parcels)
    response, status = _ok(version=version, parcels=parcels)
    response.headers["Cache-Control"] = ("private, max-age=86400"
                                         if request.args.get("v") == version
                                         else "private, no-cache")
    return response, status


@api_bp.get("/fires/alerts")
@api_login_required
def fire_alerts():
    before = request.args.get("before", type=int)
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    alerts = fire_service.member_alerts(g.uid, limit=limit, before_id=before)
    return _ok(alerts=alerts, has_more=len(alerts) == limit)


@api_bp.get("/fires/alerts/unread")
@api_login_required
def fire_alerts_unread():
    return _ok(**fire_service.member_unread(g.uid))


@api_bp.post("/fires/alerts/read")
@api_login_required
def fire_alerts_read():
    payload = request.get_json(silent=True) or {}
    try:
        last_id = int(payload.get("last_id") or 0)
    except (TypeError, ValueError):
        return _fail("Identifiant d'alerte invalide.", 400)
    return _ok(last_read_id=fire_service.mark_read(g.uid, last_id))


@api_bp.post("/fires/refresh")
@api_role_required(Roles.SUPERADMIN)
def fire_refresh():
    result = fire_service.refresh(force=True)
    if result.skipped:
        return _ok(f"Actualisation non effectuée : {result.skipped}.", skipped=result.skipped)
    return _ok(f"{result.inserted} nouveau(x) foyer(s), {result.alerts} alerte(s).",
               received=result.received, inserted=result.inserted, alerts=result.alerts,
               emailed=result.emailed)
