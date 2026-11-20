"""Manager use cases: the field agents (`agent` role) of their cooperative.

Multi-tenancy: the cooperatives always come from the database (those the
manager owns, plus a legacy user_profiles.cooperative_id), read by
account_service, never from the request."""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from core.models import Roles
from core.models.base import Row
from core.services import account_service, personnel_service
from core.services.account_service import (
    AccountServiceError, Actor, NewAccount,
)

log = logging.getLogger(__name__)


@dataclass
class AgentData:
    """What a manager provides to create an agent."""
    full_name: str
    email: str
    password: str
    mobile: Optional[str] = None
    cooperative_id: Optional[str] = None   # needed when he has several


@dataclass
class ManagerCooperative:
    """The cooperative a manager's agents belong to."""
    uuid: str
    name: Optional[str]                  # None if the row is unreadable


@dataclass
class AgentList:
    """A manager's cooperatives (the first one as `cooperative`) and their
    agents."""
    cooperative: Optional[ManagerCooperative]
    cooperatives: List[Row] = field(default_factory=list)
    agents: List[Row] = field(default_factory=list)


def _load_manager(manager_user_id: str) -> Actor:
    actor = account_service.load_actor(manager_user_id)
    if actor.role != Roles.MANAGER:
        raise AccountServiceError("Seuls les gestionnaires peuvent gérer les agents de terrain.")
    return actor


def list_agents_for_manager(manager_user_id: str) -> AgentList:
    """The manager's cooperative and its agents (newest first)."""
    actor = _load_manager(manager_user_id)
    try:
        coops = account_service.manageable_cooperatives(actor)
    except AccountServiceError:
        raise
    except Exception as exc:
        log.exception("Could not load the cooperatives of %s", manager_user_id)
        raise AccountServiceError("Impossible de charger vos agents de terrain. Veuillez "
                                  "réessayer.") from exc
    agents = account_service.list_team(actor, coops, role_name=Roles.AGENT)
    first = ManagerCooperative(coops[0]["uuid"], coops[0].get("name")) if coops else None
    return AgentList(cooperative=first, cooperatives=coops, agents=agents)


def create_agent_by_manager(manager_user_id: str, agent_data: AgentData) -> Row:
    """Create a confirmed `agent` account in the manager's cooperative; the
    username is generated from the e-mail address."""
    actor = _load_manager(manager_user_id)
    account = NewAccount(email=agent_data.email, password=agent_data.password,
                         full_name=agent_data.full_name, mobile=agent_data.mobile)
    profile = account_service.create_member(actor, account, Roles.AGENT,
                                            agent_data.cooperative_id)
    personnel_service.invalidate_overviews(actor.admin_id)   # his admin's staff page
    return profile
