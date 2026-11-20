from flask import (
    Blueprint, flash, g, redirect, render_template, request, url_for,
)

from core.forms.formulaire_forms import FormulaireInput, ResponseInput
from core.models import Roles
from core.models.choices import FORM_FIELD_TYPES
from core.routes.decorators import role_required
from core.routes.helpers import flash_errors, send_export
from core.services import formulaire_service
from core.services.errors import ServiceError
from core.services.formulaire_service import FORM_ADMINS

formulaire_bp = Blueprint("formulaires", __name__, url_prefix="/forms")

RESPONDERS = (*FORM_ADMINS, Roles.AGENT)


def _back_to_list():
    return redirect(url_for("formulaires.index"))


def _back_to_detail(formulaire_id: str):
    return redirect(url_for("formulaires.detail", formulaire_id=formulaire_id))


def _version_arg():
    return request.args.get("version", type=int)


# --------------------------------------------------------------------------
# List (administrators: their forms; agents: the forms assigned to them)
# --------------------------------------------------------------------------
@formulaire_bp.route("/")
@role_required(*RESPONDERS)
def index():
    """Forms of the admin's cooperatives, or the agent's assigned forms."""
    if g.current_role == Roles.AGENT:
        try:
            forms = formulaire_service.list_agent_forms(g.uid)
        except ServiceError as exc:
            flash(str(exc), "error")
            forms = []
        return render_template("formulaires/my_forms.html", forms=forms,
                               active_page="formulaires")

    overview = None
    try:
        overview = formulaire_service.get_admin_overview(g.uid)
    except ServiceError as exc:
        flash(str(exc), "error")
    return render_template("formulaires/list.html", overview=overview,
                           active_page="formulaires")


# --------------------------------------------------------------------------
# Builder (administrators)
# --------------------------------------------------------------------------
def _builder(formulaire_id=None):
    try:
        cooperatives, form = formulaire_service.get_builder_context(g.uid, formulaire_id)
    except ServiceError as exc:
        flash(str(exc), "error")
        return _back_to_detail(formulaire_id) if formulaire_id else _back_to_list()
    if not cooperatives:
        flash("Enregistrez d'abord une coopérative : chaque formulaire lui est rattaché.", "error")
        return redirect(url_for("main.cooperatives"))

    if request.method == "POST":
        parsed = FormulaireInput.parse(request.form)
        if parsed.ok:
            try:
                if form is None:
                    formulaire_id = formulaire_service.create_form(g.uid, parsed.data)
                    flash(f"Brouillon « {parsed.data.title} » enregistré.", "success")
                else:
                    formulaire_service.update_form(g.uid, formulaire_id, parsed.data)
                    flash("Modifications enregistrées." + (" Publiez-les pour créer une nouvelle version : les réponses déjà collectées restent dans leur version." if form["current_version"] else ""), "success")
            except ServiceError as exc:
                flash(str(exc), "error")
            else:
                return _back_to_detail(formulaire_id)
        else:
            flash_errors(parsed.errors)
        # Re-show what was typed.
        form = {**(form or {}), "title": parsed.data.title,
                "description": parsed.data.description,
                "cooperative_id": parsed.data.cooperative_id,
                "fields": parsed.data.fields}

    return render_template("formulaires/builder.html", form=form,
                           cooperatives=cooperatives, field_types=FORM_FIELD_TYPES,
                           active_page="formulaires")


@formulaire_bp.route("/new", methods=["GET", "POST"])
@role_required(*FORM_ADMINS)
def create():
    """Build a new form (saved as a draft)."""
    return _builder()


@formulaire_bp.route("/<formulaire_id>/edit", methods=["GET", "POST"])
@role_required(*FORM_ADMINS)
def edit(formulaire_id: str):
    """Edit the working copy of a form."""
    return _builder(formulaire_id)


# --------------------------------------------------------------------------
# Detail, publication, assignments (administrators)
# --------------------------------------------------------------------------
@formulaire_bp.route("/<formulaire_id>")
@role_required(*FORM_ADMINS)
def detail(formulaire_id: str):
    """Versions, field agents and tracking of one form."""
    try:
        detail_view = formulaire_service.get_form_detail(g.uid, formulaire_id)
    except ServiceError as exc:
        flash(str(exc), "error")
        return _back_to_list()
    return render_template("formulaires/detail.html", d=detail_view,
                           field_types=FORM_FIELD_TYPES, active_page="formulaires")


@formulaire_bp.route("/<formulaire_id>/publish", methods=["POST"])
@role_required(*FORM_ADMINS)
def publish(formulaire_id: str):
    """Publish the working copy as a new version."""
    try:
        version = formulaire_service.publish_form(g.uid, formulaire_id)
    except ServiceError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Version {version} publiée : le formulaire est ouvert aux agents de terrain.",
              "success")
    return _back_to_detail(formulaire_id)


@formulaire_bp.route("/<formulaire_id>/delete", methods=["POST"])
@role_required(*FORM_ADMINS)
def delete(formulaire_id: str):
    """Delete a never-published draft."""
    try:
        title = formulaire_service.delete_draft(g.uid, formulaire_id)
    except ServiceError as exc:
        flash(str(exc), "error")
        return _back_to_detail(formulaire_id)
    flash(f"Brouillon « {title} » supprimé.", "success")
    return _back_to_list()


@formulaire_bp.route("/<formulaire_id>/agents", methods=["POST"])
@role_required(*FORM_ADMINS)
def assign(formulaire_id: str):
    """Set the field agents who may answer the form."""
    try:
        count = formulaire_service.assign_agents(
            g.uid, formulaire_id, request.form.getlist("agent_ids"))
    except ServiceError as exc:
        flash(str(exc), "error")
    else:
        flash((f"{count} agents de terrain assignés." if count > 1 else f"{count} agent de terrain assigné."), "success")
    return _back_to_detail(formulaire_id)


# --------------------------------------------------------------------------
# Answering and reading responses (administrators and assigned agents)
# --------------------------------------------------------------------------
@formulaire_bp.route("/<formulaire_id>/fill", methods=["GET", "POST"])
@role_required(*RESPONDERS)
def fill(formulaire_id: str):
    """Answer the latest version of a form."""
    try:
        ctx = formulaire_service.get_fill_context(g.uid, formulaire_id)
    except ServiceError as exc:
        flash(str(exc), "error")
        return _back_to_list()

    if request.method == "POST":
        version = request.form.get("version", type=int)
        parsed = ResponseInput.parse(ctx.version["fields"], request.form, request.files)
        if parsed.ok:
            try:
                formulaire_service.submit_response(g.uid, formulaire_id, version,
                                                   parsed.data)
            except ServiceError as exc:
                flash(str(exc), "error")
            else:
                flash("Réponse enregistrée. Merci !", "success")
                if request.form.get("again") == "1":
                    return redirect(url_for("formulaires.fill",
                                            formulaire_id=formulaire_id))
                return redirect(url_for("formulaires.responses",
                                        formulaire_id=formulaire_id))
        else:
            flash_errors(parsed.errors)

    return render_template("formulaires/fill.html", ctx=ctx,
                           submitted=request.form if request.method == "POST" else {},
                           active_page="formulaires")


@formulaire_bp.route("/<formulaire_id>/responses")
@role_required(*RESPONDERS)
def responses(formulaire_id: str):
    """Responses of one version (agents: their own only)."""
    try:
        view = formulaire_service.get_responses_view(
            g.uid, formulaire_id, _version_arg(),
            request.args.get("enqueteur") or None)
    except ServiceError as exc:
        flash(str(exc), "error")
        return _back_to_list()
    return render_template("formulaires/responses.html", view=view,
                           field_types=FORM_FIELD_TYPES, active_page="formulaires")


@formulaire_bp.route("/<formulaire_id>/responses.xlsx")
@role_required(*RESPONDERS)
def export_responses(formulaire_id: str):
    """Responses of one version as an Excel sheet."""
    try:
        return send_export(formulaire_service.export_responses_xlsx(
            g.uid, formulaire_id, _version_arg(),
            request.args.get("enqueteur") or None))
    except ServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("formulaires.responses", formulaire_id=formulaire_id,
                                version=_version_arg()))
