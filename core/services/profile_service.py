"""The signed-in member's own profile and overview (dashboard)."""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.forms.validators import USERNAME_RE
from core.models import (
    CooperativeRepository, FarmerRepository, ParcelRepository,
    ProfileRepository,
)
from core.models.base import Row
from core.services.clients import optional_service_db, user_db
from core.services import page_rpc
from core.services.concurrency import Failed, parallel
from core.services.session_service import current_user

log = logging.getLogger(__name__)


@dataclass
class Overview:
    """Data of the dashboard: profile, counts and warnings to show."""
    profile: Row
    counts: Dict[str, int]
    warnings: List[str] = field(default_factory=list)


def _profile_from_auth_user(uid: str) -> Row:
    """A display-only profile built from the auth record, used when the
    user_profiles row is missing or unreadable."""
    user = current_user()
    meta = (user.user_metadata or {}) if user else {}
    email = user.email if user else None
    return {
        "uuid": uid,
        "username": meta.get("username") or (email.split("@")[0] if email else ""),
        "full_name": meta.get("full_name"),
        "email": email,
        "mobile": meta.get("mobile"),
    }


def recover_profile(uid: str) -> Optional[Row]:
    """Recreate a missing user_profiles row from the data given at signup
    (auth user_metadata). Returns the new row, or None if it can't be done
    safely.

    Needs the service key: it first checks - bypassing RLS - that no row
    exists for this uuid, so a row merely hidden by RLS is never duplicated."""
    db = optional_service_db()
    user = current_user()
    if db is None or user is None or user.id != uid:
        return None

    profiles = ProfileRepository(db)
    if profiles.get_by_uuid(uid):
        log.error("user_profiles row for %s exists but is not readable by "
                  "the member - check the table's SELECT policy", uid)
        return None

    meta = user.user_metadata or {}
    username = (meta.get("username") or "").strip()
    if not USERNAME_RE.match(username) or not user.email:
        return None

    row = profiles.insert({
        "uuid": uid,
        "username": username,
        "full_name": meta.get("full_name") or None,
        "email": user.email,
        "mobile": meta.get("mobile") or None,
    })
    log.info("Recreated missing user_profiles row for %s", uid)
    return row


def _recover_or_auth_profile(uid: str, warnings: List[str]) -> Row:
    """The profile of a member without a user_profiles row: recreated from
    the signup data if possible, else built from the auth record."""
    try:
        profile = recover_profile(uid)
    except Exception:
        log.exception("Could not recreate user_profiles row for %s", uid)
        profile = None
    if profile is None:
        warnings.append("Votre profil est incomplet. Contactez le support pour le "
                        "compléter.")
        profile = _profile_from_auth_user(uid)
    return profile


def get_overview(uid: str) -> Overview:
    """Profile and record counts for the dashboard. Never raises: a profile
    problem is reported as a warning, never as an expired session."""
    db = user_db()
    warnings: List[str] = []

    page = page_rpc.call(db, "get_my_dashboard")
    if isinstance(page, dict) and isinstance(page.get("counts"), dict):
        profile = page.get("profile")
        if profile is None:
            profile = _recover_or_auth_profile(uid, warnings)
        return Overview(profile=profile,
                        counts={k: int(page["counts"].get(k) or 0)
                                for k in ("cooperatives", "farmers", "parcels")},
                        warnings=warnings)

    # Without the migration: profile and the three counts in one round trip (each counted by
    # Postgres: no row is downloaded).
    profile_result, *count_results = parallel(
        lambda: ProfileRepository(db).get_by_uuid(uid),
        lambda: CooperativeRepository(db).count(),
        lambda: FarmerRepository(db).count(),
        lambda: ParcelRepository(db).count(),
        return_errors=True)

    profile: Optional[Row] = None
    if isinstance(profile_result, Failed):
        log.error("Could not load user_profiles row for %s: %s", uid, profile_result.error)
        warnings.append("Votre profil n'a pas pu être chargé pour le moment : certaines "
                        "informations peuvent manquer.")
        profile = _profile_from_auth_user(uid)
    else:
        profile = profile_result

    if profile is None:
        profile = _recover_or_auth_profile(uid, warnings)

    counts = {"cooperatives": 0, "farmers": 0, "parcels": 0}
    for key, result in zip(counts, count_results):
        if isinstance(result, Failed):
            # tables may not be readable yet - dashboard still renders with zeros
            log.warning("Could not count %s for the dashboard: %s", key, result.error)
        else:
            counts[key] = result

    return Overview(profile=profile, counts=counts, warnings=warnings)
