from flask import g, request

from core.models import Roles
from core.routes.api_bp import _fail, _ok, api_bp, api_login_required, api_role_required
from core.routes.helpers import send_export
from core.services import auth_service, fiche_service, kobo_service
from core.services.formulaire_service import FORM_ADMINS


def _json() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# Paramètres: KoboToolbox token
# --------------------------------------------------------------------------
@api_bp.get("/settings/kobo")
@api_role_required(*FORM_ADMINS)
def kobo_settings():
    return _ok(**kobo_service.connection_status(g.uid))


@api_bp.put("/settings/kobo")
@api_role_required(*FORM_ADMINS)
def save_kobo_settings():
    body = _json()
    status = kobo_service.save_connection(g.uid, str(body.get("server_url") or ""),
                                          str(body.get("token") or ""))
    return _ok(f"Compte KoboToolbox « {status['connection']['kobo_username']} » connecté.", **status)


@api_bp.delete("/settings/kobo")
@api_role_required(*FORM_ADMINS)
def delete_kobo_settings():
    kobo_service.delete_connection(g.uid)
    return _ok("Le jeton KoboToolbox a été supprimé de Smart Saha.")


# --------------------------------------------------------------------------
# Formulaires: Kobo import, sync and records
# --------------------------------------------------------------------------
@api_bp.get("/kobo/assets")
@api_role_required(*FORM_ADMINS)
def kobo_assets():
    return _ok(assets=kobo_service.list_assets(g.uid, refresh=request.args.get("refresh") == "1"))


@api_bp.get("/kobo/forms")
@api_role_required(*FORM_ADMINS)
def kobo_forms():
    return _ok(**kobo_service.list_forms(g.uid),
               connected=kobo_service.connection_status(g.uid)["connected"])


@api_bp.post("/kobo/forms")
@api_role_required(*FORM_ADMINS)
def kobo_import():
    body = _json()
    result = kobo_service.import_form(g.uid, str(body.get("asset_uid") or ""),
                                      body.get("cooperative_id") or None)
    return _ok(f"{result['inserted']} soumission(s) importée(s)"
               + (f", {result['duplicates']} déjà présente(s)" if result["duplicates"] else "") + ".",
               201, **result)


@api_bp.get("/kobo/forms/<form_id>")
@api_role_required(*FORM_ADMINS)
def kobo_form(form_id: str):
    return _ok(**kobo_service.form_detail(g.uid, form_id))


@api_bp.put("/kobo/forms/<form_id>")
@api_role_required(*FORM_ADMINS)
def kobo_form_update(form_id: str):
    result = kobo_service.update_form(g.uid, form_id, _json())
    located = result.get("located")
    return _ok("Réglages enregistrés." + (f" {located} enregistrement(s) placé(s) sur la carte."
                                          if located is not None else ""), **result)


@api_bp.delete("/kobo/forms/<form_id>")
@api_role_required(*FORM_ADMINS)
def kobo_form_delete(form_id: str):
    name = kobo_service.delete_form(g.uid, form_id)
    return _ok(f"« {name} » et ses données ont été supprimés de Smart Saha (rien n'est "
               "supprimé sur KoboToolbox).")


@api_bp.post("/kobo/forms/<form_id>/sync")
@api_role_required(*FORM_ADMINS)
def kobo_form_sync(form_id: str):
    r = kobo_service.sync_form(g.uid, form_id)
    if not r["received"]:
        return _ok("Déjà à jour : aucune nouvelle soumission sur KoboToolbox.", **r)
    return _ok(f"{r['inserted']} nouvelle(s) soumission(s) importée(s)"
               + (f", {r['duplicates']} doublon(s) ignoré(s)" if r["duplicates"] else "") + ".", **r)


@api_bp.get("/kobo/forms/<form_id>/records")
@api_role_required(*FORM_ADMINS)
def kobo_records(form_id: str):
    return _ok(**kobo_service.records(g.uid, form_id, request.args.get("offset", 0, type=int),
                                      request.args.get("search")))


@api_bp.post("/kobo/forms/<form_id>/records")
@api_role_required(*FORM_ADMINS)
def kobo_record_add(form_id: str):
    rid = kobo_service.save_record(g.uid, form_id, None, _json().get("data"))
    return _ok("Enregistrement ajouté.", 201, id=rid)


@api_bp.put("/kobo/forms/<form_id>/records/<record_id>")
@api_role_required(*FORM_ADMINS)
def kobo_record_edit(form_id: str, record_id: str):
    kobo_service.save_record(g.uid, form_id, record_id, _json().get("data"))
    return _ok("Enregistrement modifié.")


@api_bp.delete("/kobo/forms/<form_id>/records/<record_id>")
@api_role_required(*FORM_ADMINS)
def kobo_record_delete(form_id: str, record_id: str):
    if not kobo_service.delete_record(g.uid, form_id, record_id):
        return _fail("Cet enregistrement est introuvable.", 404)
    return _ok("Enregistrement supprimé.")


# --------------------------------------------------------------------------
# Carte: Kobo layers (every role)
# --------------------------------------------------------------------------
@api_bp.get("/kobo/layers")
@api_login_required
def kobo_layers():
    return _ok(layers=kobo_service.layers(g.uid, auth_service.lookup_role(g.uid)))


@api_bp.get("/kobo/layers/<form_id>")
@api_login_required
def kobo_layer(form_id: str):
    version, features = kobo_service.layer(g.uid, auth_service.lookup_role(g.uid), form_id)
    response, status = _ok(v=version, f=features)
    # Same URL, same data: the browser keeps the layer until it changes.
    response.headers["Cache-Control"] = ("private, max-age=86400"
                                         if request.args.get("v") == version else "private, no-cache")
    return response, status


@api_bp.get("/kobo/layers/<form_id>/records/<record_id>")
@api_login_required
def kobo_popup(form_id: str, record_id: str):
    return _ok(**kobo_service.popup(g.uid, auth_service.lookup_role(g.uid), form_id, record_id))


# --------------------------------------------------------------------------
# Parcelles: parcel sheets (managers)
# --------------------------------------------------------------------------
@api_bp.get("/fiches/sources")
@api_role_required(Roles.MANAGER)
def fiche_sources():
    return _ok(**fiche_service.sources(g.uid), templates=fiche_service.list_templates(g.uid))


@api_bp.get("/fiches/modeles")
@api_role_required(Roles.MANAGER)
def fiche_templates():
    return _ok(templates=fiche_service.list_templates(g.uid))


@api_bp.post("/fiches/modeles")
@api_role_required(Roles.MANAGER)
def fiche_template_create():
    body = _json()
    saved = fiche_service.save_template(g.uid, None, str(body.get("name") or ""), body.get("config"))
    return _ok(f"Modèle « {saved['name']} » enregistré.", 201, template=saved)


@api_bp.put("/fiches/modeles/<template_id>")
@api_role_required(Roles.MANAGER)
def fiche_template_update(template_id: str):
    body = _json()
    saved = fiche_service.save_template(g.uid, template_id, str(body.get("name") or ""),
                                        body.get("config"))
    return _ok(f"Modèle « {saved['name']} » mis à jour.", template=saved)


@api_bp.delete("/fiches/modeles/<template_id>")
@api_role_required(Roles.MANAGER)
def fiche_template_delete(template_id: str):
    fiche_service.delete_template(g.uid, template_id)
    return _ok("Modèle supprimé.")


@api_bp.post("/fiches/pdf")
@api_role_required(Roles.MANAGER)
def fiche_pdf():
    body = _json()
    if body.get("modele_id"):
        tpl = fiche_service.template(g.uid, str(body["modele_id"]))
        name, config = tpl["name"], tpl["config"]
    else:
        name, config = str(body.get("name") or "Fiche parcellaire").strip()[:120], body.get("config")
    return send_export(fiche_service.build_pdf(g.uid, name, config,
                                               fiche_service.parse_ids(body.get("parcel_ids"))))
