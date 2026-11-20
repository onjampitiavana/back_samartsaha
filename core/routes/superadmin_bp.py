from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core.forms import CreateAdminForm
from core.models import Roles
from core.routes.decorators import role_required
from core.routes.helpers import flash_form_errors
from core.services import superadmin_service
from core.services.account_service import NewAccount
from core.services.errors import ServiceError

superadmin_bp = Blueprint("superadmin", __name__, url_prefix="/superadmin")


@superadmin_bp.route("/")
@role_required(Roles.SUPERADMIN)
def dashboard():
    """Administrators overview."""
    try:
        admins = superadmin_service.list_admins()
    except ServiceError as exc:
        flash(str(exc), "error")
        admins = []
    return render_template("superadmin/dashboard.html", admins=admins,
                           active_page="superadmin")


@superadmin_bp.route("/admins/new", methods=["GET", "POST"])
@role_required(Roles.SUPERADMIN)
def create_admin():
    """Create a confirmed administrator account."""
    form = CreateAdminForm()
    if form.validate_on_submit():
        account = NewAccount(
            email=form.email.data,
            password=form.password.data,
            full_name=form.full_name.data.strip(),
            username=form.username.data.strip(),
            mobile=(form.mobile.data or "").strip() or None,
        )
        try:
            superadmin_service.create_admin(g.uid, account)
        except ServiceError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Compte administrateur créé : les identifiants ont été envoyés à "
                  f"{account.email}.", "success")
            return redirect(url_for("superadmin.dashboard"))
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template("superadmin/create_admin.html", form=form,
                           active_page="superadmin")
