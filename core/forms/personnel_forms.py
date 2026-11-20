from wtforms import EmailField, PasswordField, SelectField, StringField
from wtforms.validators import (
    DataRequired, EqualTo, Length, Optional, Regexp,
)

from core.forms.base import Form
from core.forms.validators import (
    EMAIL_MESSAGE, EMAIL_RE, MIN_PASSWORD_LENGTH, MOBILE_MESSAGE, MOBILE_RE, USERNAME_RE,
    PersonName, StrongPassword,
)
from core.models.roles import STAFF_ROLES

STAFF_ROLE_CHOICES = [(role, role.capitalize()) for role in STAFF_ROLES]


class CreateStaffForm(Form):
    """`cooperative_id.choices` must be set by the view (the actor's own
    cooperatives); the service re-checks the choice server-side. Managers
    don't join a cooperative (cooperatives are assigned to them), every
    other role needs one: account_service enforces it."""

    full_name = StringField("Full name", validators=[
        DataRequired("Saisissez le nom complet du membre."),
        Length(min=2, max=120),
        PersonName(),
    ])
    username = StringField("Username", validators=[
        DataRequired("Choisissez un nom d'utilisateur."),
        Regexp(USERNAME_RE, message="Le nom d'utilisateur doit contenir de 3 à 30 caractères "
                                    ": lettres, chiffres, « . » ou « _ »."),
    ])
    email = EmailField("Email address", validators=[
        DataRequired("Saisissez l'adresse e-mail du membre."),
        Regexp(EMAIL_RE, message=EMAIL_MESSAGE),
        Length(max=254),
    ])
    mobile = StringField("Mobile (optional)", validators=[
        Optional(),
        Regexp(MOBILE_RE, message=MOBILE_MESSAGE),
    ])
    role = SelectField("Role", choices=STAFF_ROLE_CHOICES, validators=[
        DataRequired("Choisissez un rôle."),
    ])
    cooperative_id = SelectField("Cooperative", choices=[], validators=[
        Optional(),
    ])
    password = PasswordField("Temporary password", validators=[
        DataRequired("Choisissez un mot de passe."),
        Length(min=MIN_PASSWORD_LENGTH,
               message=f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} "
                       "caractères."),
        StrongPassword(),
    ])
    confirm_password = PasswordField("Confirm password", validators=[
        DataRequired("Confirmez le mot de passe."),
        EqualTo("password", message="Les mots de passe ne correspondent pas."),
    ])


class CreateStaffNoPasswordForm(CreateStaffForm):
    """Same form for the JSON API: one optional password field, prefilled by
    the page with a generated password the administrator may edit. Empty,
    personnel_service generates one; either way it is e-mailed to the new
    member. Its strength is checked by password_service.password_problem."""
    password = StringField("Password", validators=[Optional(), Length(max=72)])
    confirm_password = None
