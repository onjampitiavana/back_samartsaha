import json
from dataclasses import asdict
from functools import wraps
from typing import Any, Callable, Iterable, Optional

from flask import Blueprint, g, jsonify, request, session
from werkzeug.datastructures import MultiDict

from core.forms import (
    CreateAdminNoPasswordForm, CreateAgentForm, CreateStaffNoPasswordForm, LoginForm,
)
from core.forms.auth_forms import PasswordChangeInput, RegistrationInput
from core.forms.formulaire_forms import FormulaireInput, ResponseInput
from core.forms.record_forms import (
    AuditInput, CampaignStatusInput, CooperativeInput, FarmerInput, ParcelDrawInput,
    ParcelUploadInput, parse_active_flag,
)
from core.models import STAFF_ROLES, Roles, role_label
from core.models.choices import (
    AUDIT_STATUS_CHOICES, CAMPAIGN_STATUS_LABELS_FR, CONTAMINATION_RISK_CHOICES,
    CONVERSION_STATUS_CHOICES,
    CROPS, FORM_FIELD_TYPES, GENDER_CHOICES, LEGAL_STATUS_CHOICES,
    MARITAL_STATUS_CHOICES, NON_CONFORMITY_SEVERITY_CHOICES, RECORD_STATUS_CHOICES,
)
from core.routes.helpers import send_export
from core.services import (
    admin_geo_service, agent_service, auth_service, cooperative_service, data_service,
    formulaire_service, parcel_service, personnel_service, profile_service,
    session_service, superadmin_service,
)
from core.services.account_service import NewAccount
from core.services.clients import user_db
from core.services.concurrency import parallel
from core.services.agent_service import AgentData
from core.services.errors import ServiceError
from core.services.formulaire_service import FORM_ADMINS
from core.services.password_service import password_problem
from core.services.session_service import SessionStatus

api_bp = Blueprint("api", __name__, url_prefix="/api")

RESPONDERS = (*FORM_ADMINS, Roles.AGENT)
PERSONNEL_MANAGERS = (Roles.ADMIN, Roles.SUPERADMIN)


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
def _ok(message: Optional[str] = None, status: int = 200, **data: Any):
    if message:
        data["message"] = message
    return jsonify(data), status


def _fail(message: Optional[str], status: int = 400,
          errors: Iterable[str] = ()):
    errors = list(errors)
    return jsonify({"message": message or (errors[0] if errors else "La demande n'a pas abouti."),
                    "errors": errors}), status


def _invalid(errors: Iterable[str]):
    """422 with every validation message, in display order."""
    return _fail(None, 422, errors)


def _form_errors(form) -> list:
    return [e for errs in form.errors.values() for e in errs]


@api_bp.errorhandler(ServiceError)
def _service_error(exc: ServiceError):
    return _fail(str(exc), 400)


@api_bp.before_request
def _require_ajax_header():
    if (request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("X-Requested-With") != "XMLHttpRequest"):
        return _fail("Requête refusée : rechargez la page puis réessayez.", 400)
    return None


# --------------------------------------------------------------------------
# Request body -> the MultiDict the HTML form parsers expect
# --------------------------------------------------------------------------
def _as_form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else ""     # what a checked / unchecked checkbox posts
    return str(value)


def _form() -> MultiDict:
    """The JSON body as form data (lists become repeated keys); multipart
    bodies are returned as they are."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return MultiDict(request.form)
    data = MultiDict()
    for key, value in payload.items():
        if value is None:
            continue
        for item in (value if isinstance(value, list) else [value]):
            data.add(key, _as_form_value(item))
    return data


def _wtform(form_class, **kwargs):
    """A Flask-WTF form bound to the JSON body. CSRF is covered by the
    X-Requested-With check above."""
    return form_class(formdata=_form(), meta={"csrf": False}, **kwargs)


# --------------------------------------------------------------------------
# Access control (JSON twins of core/routes/decorators.py)
# --------------------------------------------------------------------------
def api_login_required(view: Callable) -> Callable:
    """401 unless the Supabase session is valid; sets g.uid like
    login_required."""
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if not session_service.has_session():
            return _fail("Connectez-vous pour continuer.", 401)
        status = session_service.restore_session()
        if status is SessionStatus.UNAVAILABLE:
            return _fail("Le service de connexion est momentanément injoignable. Réessayez "
                         "dans un instant.", 503)
        if status is not SessionStatus.OK:
            session_service.end_session()
            return _fail("Votre session a expiré. Veuillez vous reconnecter.", 401)
        g.uid = g.auth_user.id
        return view(*args, **kwargs)
    return wrapped


def api_role_required(*allowed_roles: str) -> Callable:
    """403 unless the role, re-read from the database, is allowed."""
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @api_login_required
        def wrapped(*args: Any, **kwargs: Any):
            role = auth_service.lookup_role(g.uid)
            session_service.set_display_role(role)
            if role not in allowed_roles:
                return _fail("Vous n'avez pas accès à cette page.", 403)
            g.current_role = role
            return view(*args, **kwargs)
        return wrapped
    return decorator


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def _user_payload(role: Optional[str]) -> dict:
    """The signed-in member as the frontend sees it."""
    user = session_service.current_user()
    meta = (getattr(user, "user_metadata", None) or {})
    return {
        "id": getattr(user, "id", None) or session.get("user_id"),
        "email": getattr(user, "email", None),
        "role": role,
        "full_name": meta.get("full_name"),
        "username": meta.get("username"),
    }


@api_bp.get("/auth/me")
@api_login_required
def me():
    role = auth_service.lookup_role(g.uid)
    session_service.set_display_role(role)
    return _ok(user=_user_payload(role))


@api_bp.post("/auth/login")
def login():
    form = _wtform(LoginForm)
    if not form.validate():
        return _invalid(_form_errors(form))
    role = auth_service.sign_in(form.email.data, form.password.data,
                                remember=form.remember.data)
    return _ok("Connexion réussie.", user=_user_payload(role))


@api_bp.post("/auth/logout")
def logout():
    auth_service.sign_out()
    return _ok("Vous êtes déconnecté.")


@api_bp.post("/auth/signup")
def signup():
    parsed = RegistrationInput.parse(_form())
    if not parsed.ok:
        return _invalid(parsed.errors)
    result = auth_service.register(parsed.data)
    if result.signed_in:
        session_service.restore_session()
        return _ok(f"Bienvenue sur Smart Saha, {result.first_name} !", 201,
                   signed_in=True, warnings=result.warnings,
                   user=_user_payload(None))
    return _ok("Compte créé. Confirmez votre adresse depuis l'e-mail reçu, puis connectez-vous.", 201, signed_in=False,
               warnings=result.warnings)


@api_bp.post("/auth/password")
@api_login_required
def change_password():
    """Paramètres page: the signed-in member changes his own password, and
    nothing else (current password, new password, confirmation)."""
    parsed = PasswordChangeInput.parse(_form())
    if not parsed.ok:
        return _invalid(parsed.errors)
    user = session_service.current_user()
    auth_service.change_password(g.uid, getattr(user, "email", None),
                                 parsed.data.current_password, parsed.data.new_password)
    return _ok("Votre mot de passe a été modifié. Utilisez-le à votre prochaine connexion.")


# --------------------------------------------------------------------------
# Overview, cooperatives, farmers, parcels, audits
# --------------------------------------------------------------------------
@api_bp.get("/dashboard")
@api_login_required
def dashboard():
    overview = profile_service.get_overview(g.uid)
    return _ok(profile=overview.profile, counts=overview.counts,
               warnings=overview.warnings)


@api_bp.get("/cooperatives")
@api_login_required
def cooperatives():
    listing = cooperative_service.list_for_member(g.uid, auth_service.lookup_role(g.uid))
    return _ok(cooperatives=listing.cooperatives, managers=listing.managers,
               choices={"crops": CROPS, "legal_status": LEGAL_STATUS_CHOICES,
                        "status": RECORD_STATUS_CHOICES})


@api_bp.post("/cooperatives")
@api_login_required
def create_cooperative():
    form = _form()
    parsed = CooperativeInput.parse(form)
    if not parsed.ok:
        return _invalid(parsed.errors)
    cooperative_service.register_cooperative(
        g.uid, parsed.data, role=auth_service.lookup_role(g.uid),
        manager_id=form.get("manager_id") or None)
    return _ok(f"La coopérative « {parsed.data.name} » a été enregistrée.", 201)


@api_bp.put("/cooperatives/<cooperative_uuid>")
@api_login_required
def edit_cooperative(cooperative_uuid: str):
    form = _form()
    parsed = CooperativeInput.parse(form)
    if not parsed.ok:
        return _invalid(parsed.errors)
    cooperative_service.update_cooperative(
        g.uid, cooperative_uuid, parsed.data, role=auth_service.lookup_role(g.uid),
        manager_id=form.get("manager_id") or None)
    return _ok(f"La coopérative « {parsed.data.name} » a été mise à jour.")


@api_bp.patch("/cooperatives/<cooperative_uuid>/status")
@api_role_required(Roles.MANAGER, Roles.ADMIN)
def set_cooperative_status(cooperative_uuid: str):
    """JSON {is_active: true | false}: activate / deactivate a cooperative.
    Deactivated, it accepts no new farmer, parcel, audit or staff member."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("is_active")
    active = raw if isinstance(raw, bool) else parse_active_flag(str(raw or ""))
    if active is None:
        return _invalid(["Choisissez un statut valide : active ou inactive."])
    row = cooperative_service.set_active(g.uid, g.current_role, cooperative_uuid, active)
    name = row.get("name") or ""
    return _ok(f"La coopérative « {name} » a été {'activée' if active else 'désactivée'}.",
               cooperative={"uuid": row.get("uuid"), "is_active": row.get("is_active", active),
                            "status": row.get("status")})


@api_bp.delete("/cooperatives/<cooperative_uuid>")
@api_role_required(Roles.MANAGER, Roles.ADMIN)
def delete_cooperative(cooperative_uuid: str):
    """Only a deactivated cooperative with nothing attached to it."""
    name = cooperative_service.delete_cooperative(g.uid, g.current_role, cooperative_uuid)
    return _ok(f"La coopérative « {name} » a été supprimée.")


# --------------------------------------------------------------------------
# Régions / communes (admin_regions / admin_communes)
# --------------------------------------------------------------------------
@api_bp.get("/geo/regions")
@api_login_required
def geo_regions():
    """Région list of the cooperative form."""
    return _ok(**admin_geo_service.list_regions())


@api_bp.get("/geo/communes")
@api_login_required
def geo_communes():
    """Commune list, limited to ?region=<name> when given."""
    return _ok(**admin_geo_service.list_communes(request.args.get("region")))


@api_bp.get("/cooperatives/<cooperative_uuid>/location")
@api_login_required
def detect_cooperative_location(cooperative_uuid: str):
    """Région / commune from the centre of the cooperative's parcels."""
    return _ok(location=admin_geo_service.detect_for_cooperative(cooperative_uuid))


@api_bp.get("/farmers")
@api_login_required
def farmers():
    rows, coops = cooperative_service.get_farmers_page()
    for f in rows:
        coop = f.get("cooperative") or f.get("cooperatives") or {}
        f["cooperative_name"] = coop.get("name") if isinstance(coop, dict) else None
    return _ok(farmers=rows,
               cooperatives=coops,
               choices={"gender": GENDER_CHOICES,
                        "marital_status": MARITAL_STATUS_CHOICES,
                        "status": RECORD_STATUS_CHOICES})


@api_bp.post("/farmers")
@api_login_required
def create_farmer():
    parsed = FarmerInput.parse_new(_form())
    if not parsed.ok:
        return _invalid(parsed.errors)
    cooperative_service.register_farmer(g.uid, parsed.data)
    return _ok(f"Le producteur « {parsed.data.full_name} » a été enregistré.", 201)


@api_bp.delete("/farmers/<farmer_uuid>")
@api_role_required(Roles.MANAGER)
def delete_farmer(farmer_uuid: str):
    """Delete a farmer and, with him, his parcels (their audits follow)."""
    deleted = cooperative_service.delete_farmer(farmer_uuid)
    extra = ""
    if deleted.parcel_count:
        n = deleted.parcel_count
        extra = f" ainsi que {'ses' if n > 1 else 'sa'} {n} parcelle{'s' if n > 1 else ''}"
    return _ok(f"Le producteur « {deleted.full_name} »{extra} a été supprimé.",
               parcel_count=deleted.parcel_count)


@api_bp.get("/parcels")
@api_login_required
def parcels():
    """The list only: the map's outlines come from /parcels/map, fetched
    again by the page only when `map_version` changes."""
    page = parcel_service.get_parcels_page(with_map=False)
    return _ok(cooperatives=page.cooperatives,
               farmers_by_coop=page.farmers_by_coop,
               parcels=page.parcel_rows,
               map_version=page.map_version)


@api_bp.get("/parcels/map")
@api_login_required
def parcel_map():
    """Simplified outlines {parcel uuid: GeoJSON geometry}. The page asks
    for ?v=<map_version>: that URL always gives the same map, so the browser
    may keep it."""
    data = parcel_service.get_parcel_map()
    response, status = _ok(**data)
    if request.args.get("v") == data["version"]:
        response.headers["Cache-Control"] = "private, max-age=86400"
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return response, status


@api_bp.post("/parcels/import")
@api_role_required(Roles.MANAGER)
def import_parcels():
    """Multipart body with the HTML parcels page's fields: cooperative_id,
    farmer_id and parcel_file (.kml, .gpkg or zipped Shapefile)."""
    parsed = ParcelUploadInput.parse(request.form, request.files)
    if not parsed.ok:
        return _invalid(parsed.errors)
    result = parcel_service.import_parcels(parsed.data)
    s = "s" if result.count > 1 else ""
    message = (f"{result.count} parcelle{s} importée{s} depuis « "
               f"{parcel_service.uploaded_filename(parsed.data)} ».")
    if result.skipped:
        n = len(result.skipped)
        message += (f" {n} forme{'s' if n > 1 else ''} ignorée{'s' if n > 1 else ''} "
                    f"(pas des polygones) : " + " ; ".join(result.skipped[:5])
                    + (" …" if n > 5 else "") + ".")
    return _ok(message, 201, count=result.count, skipped=result.skipped)


@api_bp.post("/parcels/draw")
@api_role_required(Roles.MANAGER)
def draw_parcel():
    """JSON body {cooperative_id, farmer_id, nom?, geometry: GeoJSON Polygon}
    from the map drawing tool. Same cooperative / farmer rules as the file
    import; PostGIS normalises the shape and computes the area."""
    parsed = ParcelDrawInput.parse(request.get_json(silent=True))
    if not parsed.ok:
        return _invalid(parsed.errors)
    saved = parcel_service.save_drawn_parcel(parsed.data)
    area = float(saved.get("area_ha") or 0)
    area_text = f"{area:.4f}".replace(".", ",")
    return _ok(f"La parcelle « {saved.get('nom') or 'Parcelle'} » a été enregistrée "
               f"({area_text} ha).", 201, parcel=saved)


@api_bp.delete("/parcels/<parcel_uuid>")
@api_role_required(Roles.MANAGER)
def delete_parcel(parcel_uuid: str):
    """Delete a parcel (its audits follow)."""
    name = parcel_service.delete_parcel(parcel_uuid)
    return _ok(f"La parcelle « {name} » a été supprimée.")


@api_bp.get("/audits")
@api_login_required
def audits():
    db = user_db()
    options, rows = parallel(lambda: parcel_service.audit_parcel_options(db),
                             lambda: parcel_service.list_audits(db))
    return _ok(parcel_options=options,
               audits=rows,
               choices={"conversion_status": CONVERSION_STATUS_CHOICES,
                        "contamination_risk": CONTAMINATION_RISK_CHOICES,
                        "non_conformity_severity": NON_CONFORMITY_SEVERITY_CHOICES,
                        "overall_status": AUDIT_STATUS_CHOICES})


@api_bp.post("/audits")
@api_login_required
def create_audit():
    parsed = AuditInput.parse(_form())
    if not parsed.ok:
        return _invalid(parsed.errors)
    parcel_service.record_audit(g.uid, parsed.data)
    return _ok("Audit enregistré.", 201)


# --------------------------------------------------------------------------
# Data management
# --------------------------------------------------------------------------
EXPORTS = {
    "parcels.xlsx": data_service.export_parcels_xlsx,
    "parcels.kml": data_service.export_parcels_kml,
    "parcels.shp": data_service.export_parcels_shapefile,
    "farmers.xlsx": data_service.export_farmers_xlsx,
    "cooperatives.xlsx": data_service.export_cooperatives_xlsx,
}


@api_bp.get("/data")
@api_login_required
def data_management():
    view = data_service.get_management_view()
    return _ok(**asdict(view),
               choices={"gender": GENDER_CHOICES,
                        "marital_status": MARITAL_STATUS_CHOICES,
                        "status": RECORD_STATUS_CHOICES})


@api_bp.put("/data/farmers/<farmer_uuid>")
@api_login_required
def edit_farmer(farmer_uuid: str):
    parsed = FarmerInput.parse_edit(_form())
    if not parsed.ok:
        return _invalid(parsed.errors)
    cooperative_service.update_farmer(farmer_uuid, parsed.data)
    return _ok(f"Le producteur « {parsed.data.full_name} » a été mis à jour.")


@api_bp.post("/data/campaign-status")
@api_login_required
def update_campaign_status():
    parsed = CampaignStatusInput.parse(_form())
    if not parsed.ok:
        return _invalid(parsed.errors)
    data_service.set_campaign_status(parsed.data)
    return _ok("Statut mis à jour : "
               f"{CAMPAIGN_STATUS_LABELS_FR.get(parsed.data.new_status, parsed.data.new_status)}.")


@api_bp.get("/data/export/<name>")
@api_login_required
def export(name: str):
    build = EXPORTS.get(name)
    if build is None:
        return _fail("Cet export n'existe pas.", 404)
    return send_export(build())


# --------------------------------------------------------------------------
# Accounts: SuperAdmin -> admins, admins -> staff, managers -> agents
# --------------------------------------------------------------------------
def _new_account(form, username: bool = True) -> NewAccount:
    return NewAccount(
        email=form.email.data,
        password=form.password.data,
        full_name=form.full_name.data.strip(),
        username=form.username.data.strip() if username else None,
        mobile=(form.mobile.data or "").strip() or None,
    )


@api_bp.get("/superadmin/admins")
@api_role_required(Roles.SUPERADMIN)
def admins():
    return _ok(admins=superadmin_service.list_admins())


@api_bp.post("/superadmin/admins")
@api_role_required(Roles.SUPERADMIN)
def create_admin():
    # The password chosen on the page (generated there, editable), or none:
    # superadmin_service then generates it. Either way it is e-mailed.
    form = _wtform(CreateAdminNoPasswordForm)
    if not form.validate():
        return _invalid(_form_errors(form))
    problem = password_problem(form.password.data) if form.password.data else None
    if problem:
        return _invalid([problem])
    account = _new_account(form)
    profile = superadmin_service.create_admin(g.uid, account)
    return _ok(f"Compte administrateur créé : les identifiants ont été envoyés à "
               f"{account.email}.", 201, email=profile.get("email") or account.email)


@api_bp.get("/admin/personnel")
@api_role_required(*PERSONNEL_MANAGERS)
def personnel():
    overview = personnel_service.get_overview(g.uid, role=g.current_role)
    return _ok(cooperatives=overview.cooperatives, members=overview.members,
               managers=overview.managers, staff_roles=list(STAFF_ROLES))


@api_bp.post("/admin/personnel")
@api_role_required(*PERSONNEL_MANAGERS)
def create_personnel():
    overview = personnel_service.get_overview(g.uid, with_members=False, role=g.current_role)
    # The password chosen on the page (generated there, editable), or none:
    # personnel_service then generates it. Either way it is e-mailed.
    form = _wtform(CreateStaffNoPasswordForm)
    form.cooperative_id.choices = [(c["uuid"], c["name"])
                                   for c in overview.cooperatives]
    if not form.validate():
        return _invalid(_form_errors(form))
    password = form.password.data or ""
    problem = password_problem(password) if password else None
    if problem:
        return _invalid([problem])
    account = NewAccount(
        email=form.email.data,
        password=password,
        full_name=form.full_name.data.strip(),
        username=form.username.data.strip(),
        mobile=(form.mobile.data or "").strip() or None,
    )
    profile = personnel_service.create_staff_member(g.uid, account, form.role.data,
                                                    form.cooperative_id.data or None)
    return _ok(f"Compte « {role_label(form.role.data)} » créé : les identifiants ont été "
               f"envoyés à {account.email}.", 201,
               email=profile.get("email") or account.email)


@api_bp.delete("/admin/personnel/<member_uuid>")
@api_role_required(*PERSONNEL_MANAGERS)
def delete_personnel(member_uuid: str):
    """Delete a member of the actor's staff (checked server-side: scope,
    role, and data that the deletion would destroy)."""
    member = personnel_service.delete_staff_member(g.uid, member_uuid, role=g.current_role)
    name = member.get("full_name") or member.get("email") or "Ce membre"
    return _ok(f"Le compte « {name} » ({role_label(member.get('role'))}) a été supprimé.")


@api_bp.patch("/admin/personnel/<member_uuid>")
@api_role_required(*PERSONNEL_MANAGERS)
def update_personnel(member_uuid: str):
    """Edit name, username, mobile, cooperative of a staff member."""
    payload = request.get_json(silent=True) or {}
    member = personnel_service.update_staff_member(
        g.uid, member_uuid, {k: payload.get(k) for k in
                             ("full_name", "username", "mobile", "cooperative_id")
                             if k in payload}, role=g.current_role)
    return _ok(f"Le compte « {member.get('full_name') or member.get('email')} » a été modifié.")


@api_bp.patch("/admin/personnel/status")
@api_role_required(*PERSONNEL_MANAGERS)
def personnel_status():
    """Activate / deactivate one or several staff members: {uuids, is_active}."""
    payload = request.get_json(silent=True) or {}
    active = parse_active_flag(payload.get("is_active"))
    uuids = payload.get("uuids")
    if active is None or not isinstance(uuids, list) or not all(isinstance(u, str) for u in uuids):
        return _invalid(["Requête invalide : uuids (liste) et is_active attendus."])
    members = personnel_service.set_members_active(g.uid, uuids, active, role=g.current_role)
    verb = "réactivé" if active else "désactivé"
    if len(members) == 1:
        message = f"Le compte « {members[0].get('full_name') or members[0].get('email')} » a été {verb}."
    else:
        message = f"{len(members)} comptes ont été {verb}s."
    return _ok(message, uuids=[m["uuid"] for m in members], is_active=active)


@api_bp.post("/admin/personnel/<member_uuid>/reset-password")
@api_role_required(*PERSONNEL_MANAGERS)
def reset_personnel_password(member_uuid: str):
    """New generated password, e-mailed to the member."""
    member = personnel_service.reset_member_password(g.uid, member_uuid, role=g.current_role)
    return _ok(f"Nouveau mot de passe envoyé à {member['email']}.")


@api_bp.get("/manager/agents")
@api_role_required(Roles.MANAGER)
def agents():
    agent_list = agent_service.list_agents_for_manager(g.uid)
    return _ok(cooperative=asdict(agent_list.cooperative) if agent_list.cooperative else None,
               cooperatives=agent_list.cooperatives, agents=agent_list.agents)


@api_bp.post("/manager/agents")
@api_role_required(Roles.MANAGER)
def create_agent():
    form = _wtform(CreateAgentForm)
    if not form.validate():
        return _invalid(_form_errors(form))
    data = AgentData(
        full_name=form.full_name.data.strip(),
        email=form.email.data.strip(),
        password=form.password.data,
        mobile=(form.mobile.data or "").strip() or None,
        cooperative_id=_form().get("cooperative_id") or None,
    )
    profile = agent_service.create_agent_by_manager(g.uid, data)
    return _ok(f"Compte agent de terrain créé pour {data.email} (nom d'utilisateur : "
               f"{profile.get('username')}). L'agent peut désormais se connecter.", 201)


# --------------------------------------------------------------------------
# Survey forms
# --------------------------------------------------------------------------
def _builder_input() -> FormulaireInput:
    payload = request.get_json(silent=True) or {}
    return FormulaireInput.parse({
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "cooperative_id": str(payload.get("cooperative_id") or ""),
        "fields_json": json.dumps(payload.get("fields") or []),
    })


@api_bp.get("/forms")
@api_role_required(*RESPONDERS)
def forms():
    if g.current_role == Roles.AGENT:
        return _ok(mode="agent", forms=formulaire_service.list_agent_forms(g.uid))
    overview = formulaire_service.get_admin_overview(g.uid)
    return _ok(mode="admin", cooperatives=overview.cooperatives,
               forms=overview.forms)


@api_bp.get("/forms/builder")
@api_role_required(*FORM_ADMINS)
def builder():
    cooperatives, form = formulaire_service.get_builder_context(
        g.uid, request.args.get("id") or None)
    return _ok(cooperatives=cooperatives, form=form, field_types=FORM_FIELD_TYPES)


@api_bp.post("/forms")
@api_role_required(*FORM_ADMINS)
def create_form():
    parsed = _builder_input()
    if not parsed.ok:
        return _invalid(parsed.errors)
    formulaire_id = formulaire_service.create_form(g.uid, parsed.data)
    return _ok(f"Brouillon « {parsed.data.title} » enregistré.", 201, id=formulaire_id)


@api_bp.put("/forms/<formulaire_id>")
@api_role_required(*FORM_ADMINS)
def update_form(formulaire_id: str):
    parsed = _builder_input()
    if not parsed.ok:
        return _invalid(parsed.errors)
    _, form = formulaire_service.get_builder_context(g.uid, formulaire_id)
    formulaire_service.update_form(g.uid, formulaire_id, parsed.data)
    return _ok("Modifications enregistrées." + (" Publiez-les pour créer une nouvelle version : les réponses déjà collectées restent dans leur version." if form["current_version"] else ""),
        id=formulaire_id)


@api_bp.get("/forms/<formulaire_id>")
@api_role_required(*FORM_ADMINS)
def form_detail(formulaire_id: str):
    detail = formulaire_service.get_form_detail(g.uid, formulaire_id)
    return _ok(**asdict(detail), field_types=FORM_FIELD_TYPES)


@api_bp.post("/forms/<formulaire_id>/publish")
@api_role_required(*FORM_ADMINS)
def publish_form(formulaire_id: str):
    version = formulaire_service.publish_form(g.uid, formulaire_id)
    return _ok(f"Version {version} publiée : le formulaire est ouvert aux agents de terrain.",
               version=version)


@api_bp.delete("/forms/<formulaire_id>")
@api_role_required(*FORM_ADMINS)
def delete_form(formulaire_id: str):
    title = formulaire_service.delete_draft(g.uid, formulaire_id)
    return _ok(f"Brouillon « {title} » supprimé.")


@api_bp.put("/forms/<formulaire_id>/agents")
@api_role_required(*FORM_ADMINS)
def assign_agents(formulaire_id: str):
    count = formulaire_service.assign_agents(
        g.uid, formulaire_id, _form().getlist("agent_ids"))
    return _ok((f"{count} agents de terrain assignés." if count > 1 else f"{count} agent de terrain assigné."),
               count=count)


@api_bp.get("/forms/<formulaire_id>/fill")
@api_role_required(*RESPONDERS)
def fill_context(formulaire_id: str):
    ctx = formulaire_service.get_fill_context(g.uid, formulaire_id)
    return _ok(**asdict(ctx), is_admin=g.current_role != Roles.AGENT)


@api_bp.post("/forms/<formulaire_id>/responses")
@api_role_required(*RESPONDERS)
def submit_response(formulaire_id: str):
    """Multipart body with the HTML fill page's field names (f_<key>,
    f_<key>__lat / __lng, version, farmer_id, parcel_id, _tz_offset)."""
    ctx = formulaire_service.get_fill_context(g.uid, formulaire_id)
    parsed = ResponseInput.parse(ctx.version["fields"], request.form, request.files)
    if not parsed.ok:
        return _invalid(parsed.errors)
    response_id = formulaire_service.submit_response(
        g.uid, formulaire_id, request.form.get("version", type=int), parsed.data)
    return _ok("Réponse enregistrée. Merci !", 201, id=response_id)


@api_bp.get("/forms/<formulaire_id>/responses")
@api_role_required(*RESPONDERS)
def responses(formulaire_id: str):
    view = formulaire_service.get_responses_view(
        g.uid, formulaire_id, request.args.get("version", type=int),
        request.args.get("enqueteur") or None)
    return _ok(**asdict(view), field_types=FORM_FIELD_TYPES)


@api_bp.get("/forms/<formulaire_id>/responses.xlsx")
@api_role_required(*RESPONDERS)
def export_responses(formulaire_id: str):
    return send_export(formulaire_service.export_responses_xlsx(
        g.uid, formulaire_id, request.args.get("version", type=int),
        request.args.get("enqueteur") or None))
