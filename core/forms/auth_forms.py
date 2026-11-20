from dataclasses import dataclass
from typing import Mapping

from wtforms import BooleanField, EmailField, PasswordField
from wtforms.validators import DataRequired, Length, Regexp

from core.forms.base import Form
from core.forms.record_forms import Parsed
from core.forms.validators import (
    EMAIL_MESSAGE, EMAIL_RE, MOBILE_MESSAGE, MOBILE_RE, USERNAME_RE,
    person_name_problem,
)
from core.services.password_service import password_problem


class LoginForm(Form):
    """CSRF-protected sign-in form."""

    email = EmailField("Email address", validators=[
        DataRequired("Saisissez votre adresse e-mail."),
        Regexp(EMAIL_RE, message=EMAIL_MESSAGE),
        Length(max=254),
    ])
    password = PasswordField("Password", validators=[
        DataRequired("Saisissez votre mot de passe."),
    ])
    remember = BooleanField("Keep me signed in")


@dataclass
class RegistrationInput:
    """The /signup form (plain HTML form, parsed from request.form)."""

    full_name: str
    username: str
    email: str
    mobile: str
    password: str

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["RegistrationInput"]:
        """Unlike the other parsers, `data` is always set - the signup page
        re-fills its fields from it when there are errors."""
        data = cls(
            full_name=form.get("full_name", "").strip(),
            username=form.get("username", "").strip(),
            email=form.get("email", "").strip().lower(),
            mobile=form.get("mobile", "").strip(),
            password=form.get("password", ""),
        )
        errors = []
        problem = person_name_problem(data.full_name)
        if problem:
            errors.append(problem)
        if not USERNAME_RE.match(data.username):
            errors.append("Le nom d'utilisateur doit contenir de 3 à 30 caractères : lettres, "
                          "chiffres, « . » ou « _ ».")
        if not EMAIL_RE.match(data.email):
            errors.append(EMAIL_MESSAGE)
        if not MOBILE_RE.match(data.mobile):
            errors.append(MOBILE_MESSAGE)
        problem = password_problem(data.password)
        if problem:
            errors.append(problem)
        if data.password != form.get("confirm_password", ""):
            errors.append("Les mots de passe ne correspondent pas.")
        return Parsed(data, errors)

    def refill(self) -> dict:
        """Template values to re-fill the form (never the password)."""
        return dict(full_name=self.full_name, username=self.username,
                    email=self.email, mobile=self.mobile)


@dataclass
class PasswordChangeInput:
    """The password change form of the Paramètres page."""

    current_password: str
    new_password: str

    @classmethod
    def parse(cls, form: Mapping[str, str]) -> Parsed["PasswordChangeInput"]:
        """Validate the posted form; errors come back in display order."""
        current = form.get("current_password", "")
        new = form.get("new_password", "")
        confirm = form.get("confirm_password", "")
        errors = []
        if not current:
            errors.append("Saisissez votre mot de passe actuel.")
        if not new:
            errors.append("Saisissez le nouveau mot de passe.")
        else:
            problem = password_problem(new)
            if problem:
                errors.append(problem)
            elif len(new) > 72:
                errors.append("Le mot de passe ne doit pas dépasser 72 caractères.")
            elif new == current:
                errors.append("Le nouveau mot de passe doit être différent de l'actuel.")
        if new != confirm:
            errors.append("La confirmation ne correspond pas au nouveau mot de passe.")
        if errors:
            return Parsed(None, errors)
        return Parsed(cls(current, new))
