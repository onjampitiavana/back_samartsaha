from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core.forms.agent_form import CreateAgentForm
from core.models import Roles
from core.routes.decorators import role_required
from core.routes.helpers import flash_form_errors
from core.services import agent_service
from core.services.agent_service import AgentData
from core.services.errors import ServiceError

manager_bp = Blueprint("manager", __name__, url_prefix="/manager")


@manager_bp.route("/agents")
@role_required(Roles.MANAGER)
def agents():
    """Field agents of the manager's cooperative."""
    agent_list = None
    try:
        agent_list = agent_service.list_agents_for_manager(g.uid)
    except ServiceError as exc:
        flash(str(exc), "error")
    return render_template("manager/agents/list.html", agent_list=agent_list,
                           active_page="agents")


@manager_bp.route("/agents/create", methods=["GET", "POST"])
@role_required(Roles.MANAGER)
def create_agent():
    """Create a confirmed agent account in the manager's cooperative."""
    # Resolve the cooperative first: no point showing the form if the
    # manager can't create agents (no cooperative...).
    try:
        agent_list = agent_service.list_agents_for_manager(g.uid)
    except ServiceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("manager.agents"))

    form = CreateAgentForm()
    if form.validate_on_submit():
        data = AgentData(
            full_name=form.full_name.data.strip(),
            email=form.email.data.strip(),
            password=form.password.data,
            mobile=(form.mobile.data or "").strip() or None,
        )
        try:
            profile = agent_service.create_agent_by_manager(g.uid, data)
        except ServiceError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Compte agent de terrain créé pour {data.email} (nom d'utilisateur : "
                  f"{profile.get('username')}). L'agent peut désormais se connecter.", "success")
            return redirect(url_for("manager.agents"))
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template("manager/agents/create.html", form=form,
                           cooperative=agent_list.cooperative,
                           active_page="agents")
