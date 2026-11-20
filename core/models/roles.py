"""Role names (public.roles.role) and the account-creation hierarchy."""

from typing import Dict, FrozenSet, Optional


class Roles:
    """Role names exactly as stored in public.roles.role."""

    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    AUDITOR = "auditor"
    CONSULTANT = "consultant"


# Which roles each role may create. Cooperative scoping is decided by the
# account service (see core/services/account_service.py).
CREATABLE_ROLES: Dict[str, FrozenSet[str]] = {
    Roles.SUPERADMIN: frozenset({Roles.ADMIN, Roles.MANAGER, Roles.AGENT,
                                 Roles.AUDITOR, Roles.CONSULTANT}),
    Roles.ADMIN: frozenset({Roles.MANAGER, Roles.AGENT, Roles.AUDITOR,
                            Roles.CONSULTANT}),
    Roles.MANAGER: frozenset({Roles.AGENT}),
}

# Staff roles an admin (or the SuperAdmin) assigns, in display order.
# Managers belong to their admin (user_profiles.admin_id) and receive
# cooperatives; the other roles join one of those cooperatives.
STAFF_ROLES = (Roles.MANAGER, Roles.AGENT, Roles.AUDITOR, Roles.CONSULTANT)


# How each role is named to users (messages, e-mails).
ROLE_LABELS_FR: Dict[str, str] = {
    Roles.SUPERADMIN: "Super administrateur",
    Roles.ADMIN: "Administrateur",
    Roles.MANAGER: "Gestionnaire",
    Roles.AGENT: "Agent de terrain",
    Roles.AUDITOR: "Auditeur",
    Roles.CONSULTANT: "Consultant",
}


def role_label(role: Optional[str]) -> str:
    """French name of `role` ("Agent de terrain"), or the raw name."""
    return ROLE_LABELS_FR.get(role or "", role or "")


def can_create(actor_role: Optional[str], target_role: str) -> bool:
    """True if a member with `actor_role` may create a `target_role` account."""
    return target_role in CREATABLE_ROLES.get(actor_role or "", frozenset())
