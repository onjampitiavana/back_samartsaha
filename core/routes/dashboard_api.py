from flask import g, request

from core.models import Roles
from core.routes.api_bp import _ok, api_bp, api_role_required
from core.services import dashboard_service

DASHBOARD_ROLES = (Roles.ADMIN, Roles.MANAGER)


@api_bp.get("/dashboard/map")
@api_role_required(*DASHBOARD_ROLES)
def dashboard_map():
    filters = dashboard_service.DashboardFilters.from_args(request.args)
    response, _ = _ok(**dashboard_service.member_dashboard(g.uid, g.current_role, filters))
    # Private to the member; revalidated on every visit. The ETag turns an
    # unchanged scope into a bodiless 304 (the browser reuses its copy).
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Vary"] = "Cookie"
    response.add_etag()
    return response.make_conditional(request)
