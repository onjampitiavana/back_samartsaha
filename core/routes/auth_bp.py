from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for,
)

from core.forms.auth_forms import LoginForm, RegistrationInput
from core.routes.helpers import flash_errors, flash_form_errors
from core.services import auth_service, session_service
from core.services.errors import ServiceError

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/")
def index():
    """Send members to their home page, visitors to the sign-in page."""
    if session_service.has_session():
        return redirect(url_for(auth_service.home_endpoint_for(session.get("role"))))
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Sign-in form; lands each role on its own home page."""
    form = LoginForm()
    if form.validate_on_submit():
        try:
            role = auth_service.sign_in(form.email.data, form.password.data,
                                        remember=form.remember.data)
        except ServiceError as exc:
            flash(str(exc), "error")
        else:
            flash("Connexion réussie.", "success")
            return redirect(url_for(auth_service.home_endpoint_for(role)))
    elif request.method == "POST":
        flash_form_errors(form)
    return render_template("auth/login.html", form=form)


@auth_bp.route("/signin", methods=["GET", "POST"])
def signin():
    """Old address of the sign-in page, kept for bookmarks and links."""
    return redirect(url_for("auth.login"), code=307)


@auth_bp.route("/logout")
def logout():
    """Sign the member out and go back to the sign-in page."""
    auth_service.sign_out()
    flash("Vous êtes déconnecté.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Public registration of a member account."""
    if request.method == "GET":
        return render_template("signup.html")

    parsed = RegistrationInput.parse(request.form)
    if not parsed.ok:
        flash_errors(parsed.errors)
        return render_template("signup.html", **parsed.data.refill())

    try:
        result = auth_service.register(parsed.data)
    except ServiceError as exc:
        flash(str(exc), "error")
        return render_template("signup.html", **parsed.data.refill())

    flash_errors(result.warnings)
    if result.signed_in:
        flash(f"Bienvenue sur Smart Saha, {result.first_name} !", "success")
        return redirect(url_for("main.dashboard"))
    flash("Compte créé. Confirmez votre adresse depuis l'e-mail reçu, puis connectez-vous.", "success")
    return redirect(url_for("auth.login"))
