"""Field agent creation form (managers)."""

from wtforms import EmailField, PasswordField, StringField
from wtforms.validators import DataRequired, EqualTo, Length, Optional, Regexp

from core.forms.base import Form
from core.forms.validators import (
    EMAIL_MESSAGE, EMAIL_RE, MIN_PASSWORD_LENGTH, MOBILE_MESSAGE, MOBILE_RE,
    PersonName, StrongPassword,
)


class CreateAgentForm(Form):
    """No cooperative field on purpose: the agent always joins the manager's
    own cooperative, read server-side by agent_service."""

    full_name = StringField("Full name", validators=[
        DataRequired("Saisissez le nom complet de l'agent."),
        Length(min=2, max=120),
        PersonName(),
    ])
    email = EmailField("Email address", validators=[
        DataRequired("Saisissez l'adresse e-mail de l'agent."),
        Regexp(EMAIL_RE, message=EMAIL_MESSAGE),
        Length(max=254),
    ])
    mobile = StringField("Field phone (optional)", validators=[
        Optional(),
        Regexp(MOBILE_RE, message=MOBILE_MESSAGE),
    ])
    password = PasswordField("Initial password", validators=[
        DataRequired("Choisissez un mot de passe initial."),
        Length(min=MIN_PASSWORD_LENGTH,
               message=f"Le mot de passe doit contenir au moins {MIN_PASSWORD_LENGTH} "
                       "caractères."),
        StrongPassword(),
    ])
    confirm_password = PasswordField("Confirm password", validators=[
        DataRequired("Confirmez le mot de passe."),
        EqualTo("password", message="Les mots de passe ne correspondent pas."),
    ])
