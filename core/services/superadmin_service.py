"""SuperAdmin use cases: list and create administrator accounts."""

from typing import List

from core.models import Roles
from core.models.base import Row
from core.services import account_service, personnel_service
from core.services.account_service import NewAccount
from core.services.password_service import generate_password

CREDENTIALS_SUBJECT = "Vos accès administrateur Smart Saha"
# Product requirement: this exact text.
CREDENTIALS_BODY = (
    "Bonjour, vous êtes inscrit en tant qu'administrateur de Smart Saha, veuillez vous "
    "connecter avec votre mail et mot de passe. \n"
    "Mail : {email} \n"
    "Mot de passe : {password}"
)


def list_admins() -> List[Row]:
    """Every administrator profile, newest first."""
    return account_service.list_by_role(Roles.ADMIN)


def create_admin(actor_uid: str, account: NewAccount) -> Row:
    """Create a confirmed `admin` account and e-mail them their credentials.
    The actor's role is re-read from the database and must be allowed to
    create admins (superadmin).

      1. the address must be able to receive mail, else "Adresse email
         introuvable";
      2. the password is generated unless the caller chose one;
      3. the account is created, then the credentials are e-mailed; if they
         can't be delivered the account is deleted again."""
    personnel_service.ensure_deliverable(account.email)

    actor = account_service.load_actor(actor_uid)
    account.password = account.password or generate_password()
    profile = account_service.create_member(actor, account, Roles.ADMIN)

    email = profile.get("email") or account.email
    body = CREDENTIALS_BODY.format(email=email, password=account.password)
    personnel_service.send_credentials_or_rollback(profile, email, CREDENTIALS_SUBJECT, body)
    return profile
