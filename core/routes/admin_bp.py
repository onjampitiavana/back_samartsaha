from typing import Optional

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core.forms.personnel_forms import CreateStaffForm
from core.models import Roles, role_label
from core.routes.decorators import role_required
from core.routes.helpers import flash_form_errors
from core.services import personnel_service
from core.services.account_service import NewAccount
from core.services.errors import ServiceError
from core.services.personnel_service import PersonnelOverview

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PERSONNEL_MANAGERS = (Roles.ADMIN, Roles.SUPERADMIN)


def _overview() -> Optional[PersonnelOverview]:
    """The actor's cooperatives and staff, or None after flashing why not."""
    try:
        return personnel_service.get_overview(g.uid)
    except ServiceError as exc:
        flash(str(exc), "error")
        return None

@admin_bp.route("/personnel/")
@role_required(*PERSONNEL_MANAGERS)
def personnel():
    """Staff (managers, auditors, consultants) of the actor's cooperatives."""
    return render_template("admin/personnel.html", overview=_overview(),
                           active_page="personnel")
@admin_bp.route("/personnel/new", methods=["GET", "POST"])
@role_required(*PERSONNEL_MANAGERS)
def create_personnel():
    """Create a staff account in one of the actor's cooperatives."""
    overview = _overview()
    if overview is None:
        return redirect(url_for("admin.personnel"))   # the list explains why

    form = CreateStaffForm()
    form.cooperative_id.choices = [(c["uuid"], c["name"])
                                   for c in overview.cooperatives]
    if form.validate_on_submit():
        account = NewAccount(
            email=form.email.data,
            password=form.password.data,
            full_name=form.full_name.data.strip(),
            username=form.username.data.strip(),
            mobile=(form.mobile.data or "").strip() or None,
        )
        try:
            personnel_service.create_staff_member(
                g.uid, account, form.role.data, form.cooperative_id.data)
        except ServiceError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Compte « {role_label(form.role.data)} » créé pour {account.email} : la "
                  "personne peut désormais se connecter.", "success")
            return redirect(url_for("admin.personnel"))
    elif request.method == "POST":
        flash_form_errors(form)

    return render_template("admin/create_personnel.html", form=form,
                           active_page="personnel")
