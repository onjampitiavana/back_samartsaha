
import re
from typing import Any, Dict, List, Optional

from core.models.base import BaseRepository, Row

_USERNAME_CHARS = re.compile(r"[^a-zA-Z0-9_.]")

PROFILE_LIST_COLUMNS = ("uuid, username, full_name, email, mobile, role_id, "
                        "cooperative_id, admin_id, created_at")


class RoleRepository(BaseRepository):
    """public.roles: role names and ids.

    The table is a fixed list read on almost every request, so it is loaded
    once per process (and again only if a role is missing from it)."""
    TABLE = "roles"
    _names: Optional[Dict[int, str]] = None

    def id_to_name(self, reload: bool = False) -> Dict[int, str]:
        """Every role as {id: name}."""
        if reload or RoleRepository._names is None:
            RoleRepository._names = {
                r["id"]: r["role"]
                for r in self.rows(self.query().select("id, role").execute())}
        return dict(RoleRepository._names)

    def get_id(self, role_name: str) -> Optional[int]:
        """Id of the role named `role_name`, or None."""
        for reload in (False, True):
            found = next((i for i, n in self.id_to_name(reload).items()
                          if n == role_name), None)
            if found is not None:
                return found
        return None

    def get_name(self, role_id: Optional[int]) -> Optional[str]:
        """Name of the role `role_id`, or None."""
        if role_id is None:
            return None
        names = self.id_to_name()
        if role_id not in names:
            names = self.id_to_name(reload=True)
        return names.get(role_id)


class ProfileRepository(BaseRepository):
    """public.user_profiles: member profiles, roles and cooperative links."""
    TABLE = "user_profiles"
    # False once PostgREST says user_profiles has no relationship to roles.
    _embed_roles = True

    def get_by_uuid(self, uid: str) -> Optional[Row]:
        """The profile of auth user `uid`, or None."""
        return self.first(self.query().select("*").eq("uuid", uid)
                          .limit(1).execute())

    def get_by_email(self, email: str) -> Optional[Row]:
        # Signup stores e-mails lower-cased; eq() avoids ilike's '_' wildcard.
        """The profile with this e-mail (case-insensitive), or None."""
        return self.first(self.query().select("*")
                          .eq("email", email.strip().lower())
                          .limit(1).execute())

    def username_taken(self, username: str) -> bool:
        """True if a profile already uses `username`."""
        return bool(self.rows(self.query().select("uuid")
                              .eq("username", username).limit(1).execute()))

    def get_role_name(self, uid: str) -> Optional[str]:
        """Role name of a member; None without a profile or a role.

        One round trip: the role name comes embedded with the profile
        (user_profiles.role_id -> roles.id), which also fills the roles
        cache. Without that relationship in PostgREST, two queries as
        before (the roles table is then read once per process)."""
        if ProfileRepository._embed_roles:
            try:
                row = self.first(self.query().select("role_id, roles(role)")
                                 .eq("uuid", uid).limit(1).execute())
            except Exception as exc:
                if "PGRST200" not in str(exc) and "relationship" not in str(exc).lower():
                    raise
                ProfileRepository._embed_roles = False
            else:
                if not row:
                    return None
                embedded = row.get("roles")
                if isinstance(embedded, list):
                    embedded = embedded[0] if embedded else None
                if isinstance(embedded, dict) and embedded.get("role"):
                    return embedded["role"]
                return RoleRepository(self.client).get_name(row.get("role_id"))
        row = self.first(self.query().select("role_id").eq("uuid", uid)
                         .limit(1).execute())
        if not row:
            return None
        return RoleRepository(self.client).get_name(row.get("role_id"))

    def role_by_email(self, email: str) -> Optional[Row]:
        """{uuid, role} of the profile with this e-mail, or None (several
        profiles, none, or no role). Used to read the role while the
        password is being checked: the caller keeps it only if `uuid` is
        the member who actually signed in."""
        rows = self.rows(self.query().select("uuid, role_id, roles(role)")
                         .eq("email", email).limit(2).execute())
        if len(rows) != 1:
            return None
        embedded = rows[0].get("roles")
        if isinstance(embedded, list):
            embedded = embedded[0] if embedded else None
        role = embedded.get("role") if isinstance(embedded, dict) else None
        return {"uuid": rows[0].get("uuid"), "role": role} if role else None

    def list_by_role(self, role_id: int) -> List[Row]:
        """Profiles with `role_id`, newest first."""
        return self.rows(self.query().select(PROFILE_LIST_COLUMNS)
                         .eq("role_id", role_id)
                         .order("created_at", desc=True).execute())

    def list_by_cooperatives(self, cooperative_ids: List[str],
                             role_id: Optional[int] = None) -> List[Row]:
        """Profiles attached to any of `cooperative_ids`, optionally of one
        role, newest first."""
        if not cooperative_ids:
            return []
        query = (self.query().select(PROFILE_LIST_COLUMNS)
                 .in_("cooperative_id", cooperative_ids))
        if role_id is not None:
            query = query.eq("role_id", role_id)
        return self.rows(query.order("created_at", desc=True).execute())

    def list_by_uuids(self, uids: List[str]) -> List[Row]:
        """Profiles of `uids` (any order)."""
        if not uids:
            return []
        return self.rows(self.query().select(PROFILE_LIST_COLUMNS).in_("uuid", uids).execute())

    def list_by_admin(self, admin_uid: str,
                      role_id: Optional[int] = None) -> List[Row]:
        """Profiles whose referent administrator is `admin_uid`, optionally
        of one role, newest first."""
        query = self.query().select(PROFILE_LIST_COLUMNS).eq("admin_id", admin_uid)
        if role_id is not None:
            query = query.eq("role_id", role_id)
        return self.rows(query.order("created_at", desc=True).execute())

    def set_role(self, uid: str, role_id: int) -> None:
        """Give the member `uid` the role `role_id`."""
        self.query().update({"role_id": role_id}).eq("uuid", uid).execute()

    def available_username(self, seed: str) -> str:
        base = _USERNAME_CHARS.sub("", seed.split("@")[0])[:26]
        if len(base) < 3:
            base = (base + "user")[:26]
        candidate, n = base, 1
        while self.username_taken(candidate):
            n += 1
            candidate = f"{base}_{n}"
        return candidate


class CooperativeRepository(BaseRepository):
    """public.cooperative."""
    TABLE = "cooperative"

    def get(self, cooperative_uuid: str) -> Optional[Row]:
        """uuid + name of one cooperative, or None."""
        return self.first(self.query().select("uuid, name")
                          .eq("uuid", cooperative_uuid).limit(1).execute())

    def list_all(self) -> List[Row]:
        """Full rows, newest first (cooperatives page)."""
        return self.rows(self.query().select("*")
                         .order("created_at", desc=True).execute())

    def list_names(self) -> List[Row]:
        """uuid + name, alphabetical (select options)."""
        return self.rows(self.query().select("uuid, name").order("name").execute())

    def list_owned_by(self, owner_uid: str) -> List[Row]:
        """Cooperatives registered by this member (cooperative.owner)."""
        return self.list_owned_by_any([owner_uid])

    def list_owned_by_any(self, owner_uids: List[str],
                          columns: str = "uuid, name, owner") -> List[Row]:
        """Cooperatives belonging to any of `owner_uids`, alphabetical."""
        if not owner_uids:
            return []
        return self.rows(self.query().select(columns)
                         .in_("owner", owner_uids).order("name").execute())

    def update(self, cooperative_uuid: str, values: Row) -> List[Row]:
        """Update one row by uuid; returns the updated rows ([] if the row
        doesn't exist or isn't writable by the client)."""
        return self.rows(self.query().update(values).eq("uuid", cooperative_uuid).execute())

    def list_for_export(self) -> List[Row]:
        """Full rows, alphabetical (export)."""
        return self.rows(self.query().select("*").order("name").execute())

    def name_lookup(self) -> Dict[str, str]:
        """Every cooperative as {uuid: name}."""
        return {c["uuid"]: c["name"]
                for c in self.rows(self.query().select("uuid, name").execute())}



class FarmerRepository(BaseRepository):
    """public.farmers."""
    TABLE = "farmers"

    def list_with_cooperative(self) -> List[Row]:
        """Newest first, with the cooperative name joined."""
        return self.rows(self.query()
                         .select("*, cooperative!fk_farmer_coop(name)")
                         .order("created_at", desc=True).execute())

    def list_options(self) -> List[Row]:
        """uuid, name and cooperative of every farmer (select options)."""
        return self.rows(self.query().select("uuid, full_name, cooperative_id")
                         .order("full_name").execute())

    def list_by_name(self) -> List[Row]:
        """Full rows, alphabetical (data page, export)."""
        return self.rows(self.query().select("*").order("full_name").execute())

    def list_by_cooperative(self, cooperative_id: str) -> List[Row]:
        """uuid + name of a cooperative's farmers, alphabetical."""
        return self.rows(self.query().select("uuid, full_name")
                         .eq("cooperative_id", cooperative_id)
                         .order("full_name").execute())

    def update(self, farmer_uuid: str, values: Row) -> None:
        """Update one row by uuid."""
        self.query().update(values).eq("uuid", farmer_uuid).execute()

    def get(self, farmer_uuid: str) -> Optional[Row]:
        """uuid + name of one farmer visible to the client, or None."""
        return self.first(self.query().select("uuid, full_name")
                          .eq("uuid", farmer_uuid).limit(1).execute())



class ParcelRepository(BaseRepository):
    """public.parcels and the parcel RPCs."""
    TABLE = "parcels"

    def list_for_management(self) -> List[Row]:
        """Columns shown on the data page, newest first."""
        return self.rows(self.query()
                         .select("uuid, nom, cooperative_id, farmer_id, area_ha, "
                                 "bio_campaign_status, created_at")
                         .order("created_at", desc=True).execute())

    def list_by_cooperative(self, cooperative_id: str) -> List[Row]:
        """uuid, name, farmer and area of a cooperative's parcels, newest first."""
        return self.rows(self.query().select("uuid, nom, farmer_id, area_ha")
                         .eq("cooperative_id", cooperative_id)
                         .order("created_at", desc=True).execute())

    def name_lookup(self, parcel_uuids: List[str]) -> Dict[str, str]:
        """{uuid: nom} of the given parcels."""
        if not parcel_uuids:
            return {}
        rows = self.rows(self.query().select("uuid, nom")
                         .in_("uuid", parcel_uuids).execute())
        return {r["uuid"]: r["nom"] for r in rows}

    def update(self, parcel_uuid: str, values: Row) -> None:
        """Update one row by uuid."""
        self.query().update(values).eq("uuid", parcel_uuid).execute()

    def get(self, parcel_uuid: str) -> Optional[Row]:
        """uuid + name of one parcel visible to the client, or None."""
        return self.first(self.query().select("uuid, nom")
                          .eq("uuid", parcel_uuid).limit(1).execute())


    # -- RPCs --------------------------------------------------------------
    def insert_geometry(self, cooperative_id: str, farmer_id: str,
                        geojson: Dict[str, Any], nom: Optional[str] = None) -> None:
        """insert_parcel() checks ownership and sets owner = auth.uid().
        Without `nom` the database names it "Parcelle <start of uuid>"."""
        self.client.rpc("insert_parcel", {
            "coop_id": cooperative_id,
            "farmer_id_in": farmer_id,
            "geom_geojson": geojson,
            "nom_in": nom,
        }).execute()

    def insert_geometries(self, cooperative_id: str, farmer_id: str,
                          features: List[Dict[str, Any]]) -> int:
        """insert_parcels(): every feature ({geometry, nom}) in one call and
        one transaction (all or nothing); returns how many were stored."""
        return int(self.client.rpc("insert_parcels", {
            "coop_id": cooperative_id,
            "farmer_id_in": farmer_id,
            "features": [{"geometry": f["geometry"], "nom": f.get("nom")} for f in features],
        }).execute().data or 0)

    def insert_drawn(self, cooperative_id: str, farmer_id: str,
                     geojson: Dict[str, Any], nom: Optional[str] = None) -> Row:
        """insert_drawn_parcel(): one polygon drawn on the map, normalised and
        measured by PostGIS like an imported one; returns {uuid, nom,
        area_ha, kind}."""
        return self.client.rpc("insert_drawn_parcel", {
            "coop_id": cooperative_id,
            "farmer_id_in": farmer_id,
            "geom_geojson": geojson,
            "nom_in": nom,
        }).execute().data or {}

    def list_mine(self) -> List[Row]:
        """get_my_parcels(): the caller's parcels with names and GeoJSON."""
        return self.rows(self.client.rpc("get_my_parcels", {}).execute())


class AuditRepository(BaseRepository):
    """public.audits_bio and its RPCs (get_my_audits, and the Audit Bio
    page RPCs of migrations/20261122_audits_bio_web.sql)."""
    TABLE = "audits_bio"

    def list_mine(self) -> List[Row]:
        """Rows returned by the caller-scoped RPC."""
        return self.rows(self.client.rpc("get_my_audits", {}).execute())

    def page(self) -> Row:
        """get_audits_bio_page(): {parcels, audits, drafts} of the caller."""
        return self.client.rpc("get_audits_bio_page", {}).execute().data or {}

    def parcel_context(self, parcel_uuid: str) -> Optional[Row]:
        """audit_bio_parcel_context(): identification data of one of the
        caller's parcels, or None."""
        return self.client.rpc("audit_bio_parcel_context",
                               {"p_parcel": parcel_uuid}).execute().data or None

    def get(self, audit_uuid: str) -> Optional[Row]:
        """One audit visible to the caller (signature included), or None."""
        return self.first(self.query().select("*").eq("uuid", audit_uuid)
                          .limit(1).execute())

    def list_for_parcel(self, parcel_uuid: str) -> List[Row]:
        """Every audit of a parcel visible to the caller, newest first,
        without the signature."""
        rows = self.rows(self.query().select("*").eq("parcel_id", parcel_uuid)
                         .order("audit_date", desc=True)
                         .order("created_at", desc=True).execute())
        for row in rows:
            row.pop("auditor_signature", None)
        return rows

    def set_non_conformities(self, audit_uuid: str, items: List[Row]) -> List[Row]:
        """Store the follow-up of the non-conformities (the only change the
        database accepts on a recorded audit)."""
        return self.rows(self.query().update({"non_conformity_items": items})
                         .eq("uuid", audit_uuid).execute())


class AuditDraftRepository(BaseRepository):
    """public.audits_bio_drafts: audits in progress (owner only)."""
    TABLE = "audits_bio_drafts"

    def get(self, draft_uuid: str) -> Optional[Row]:
        """One of the caller's drafts, or None."""
        return self.first(self.query().select("*").eq("uuid", draft_uuid)
                          .limit(1).execute())

    def update(self, draft_uuid: str, values: Row) -> List[Row]:
        """Update one draft; returns the updated rows ([] if not his)."""
        return self.rows(self.query().update(values).eq("uuid", draft_uuid).execute())


class CampaignRepository(BaseRepository):
    """Bio campaign status of farmers / parcels (RPC only)."""

    def log(self) -> List[Row]:
        """get_campaign_log(): history of status changes."""
        return self.rows(self.client.rpc("get_campaign_log", {}).execute())

    def set_status(self, entity_type: str, entity_id: str, new_status: str,
                   reason: Optional[str]) -> None:
        """set_campaign_status(): mark an entity considered / banned."""
        self.client.rpc("set_campaign_status", {
            "entity_type_in": entity_type,
            "entity_id_in": entity_id,
            "new_status_in": new_status,
            "reason_in": reason,
        }).execute()


class FormulaireRepository(BaseRepository):
    """public.formulaires and the survey form RPCs (service role only)."""
    TABLE = "formulaires"

    def get(self, formulaire_id: str) -> Optional[Row]:
        """One form, or None."""
        return self.first(self.query().select("*").eq("id", formulaire_id)
                          .limit(1).execute())

    def list_by_cooperatives(self, cooperative_ids: List[str]) -> List[Row]:
        """Forms of any of `cooperative_ids`, most recently changed first."""
        if not cooperative_ids:
            return []
        return self.rows(self.query().select("*")
                         .in_("cooperative_id", cooperative_ids)
                         .order("updated_at", desc=True).execute())

    def list_published_by_ids(self, formulaire_ids: List[str]) -> List[Row]:
        """Published forms among `formulaire_ids`, alphabetical."""
        if not formulaire_ids:
            return []
        return self.rows(self.query().select("*").in_("id", formulaire_ids)
                         .eq("status", "published").order("title").execute())

    def update(self, formulaire_id: str, values: Row) -> None:
        """Update one form by id."""
        self.query().update(values).eq("id", formulaire_id).execute()

    def delete(self, formulaire_id: str) -> None:
        """Delete one form by id (only ever-unpublished forms: versions
        reference it with ON DELETE RESTRICT)."""
        self.query().delete().eq("id", formulaire_id).execute()

    # -- RPCs --------------------------------------------------------------
    def publish(self, formulaire_id: str, actor_uid: str) -> int:
        """publish_formulaire(): freeze the fields, create the version's
        response table; returns the new version number."""
        return self.client.rpc("publish_formulaire", {
            "p_formulaire_id": formulaire_id,
            "p_actor": actor_uid,
        }).execute().data

    def insert_response(self, formulaire_id: str, version: int,
                        enqueteur_id: str, farmer_id: Optional[str],
                        parcel_id: Optional[str], values: Row) -> str:
        """insert_formulaire_response(): returns the new response id."""
        return self.client.rpc("insert_formulaire_response", {
            "p_formulaire_id": formulaire_id,
            "p_version": version,
            "p_enqueteur_id": enqueteur_id,
            "p_farmer_id": farmer_id,
            "p_parcel_id": parcel_id,
            "p_values": values,
        }).execute().data

    def responses(self, formulaire_id: str, version: int,
                  enqueteur_id: Optional[str] = None) -> List[Row]:
        """get_formulaire_responses(): newest first."""
        return list(self.client.rpc("get_formulaire_responses", {
            "p_formulaire_id": formulaire_id,
            "p_version": version,
            "p_enqueteur_id": enqueteur_id,
        }).execute().data or [])

    def stats(self, formulaire_id: str) -> Row:
        """get_formulaire_stats(): totals per version and per agent."""
        return self.client.rpc("get_formulaire_stats", {
            "p_formulaire_id": formulaire_id,
        }).execute().data or {}


class FormulaireVersionRepository(BaseRepository):
    """public.formulaire_versions: published, frozen field lists."""
    TABLE = "formulaire_versions"

    def list_for(self, formulaire_id: str) -> List[Row]:
        """Every version of a form, newest first."""
        return self.rows(self.query()
                         .select("version, fields, table_name, published_by, published_at")
                         .eq("formulaire_id", formulaire_id)
                         .order("version", desc=True).execute())

    def get(self, formulaire_id: str, version: int) -> Optional[Row]:
        """One version of a form, or None."""
        return self.first(self.query().select("*")
                          .eq("formulaire_id", formulaire_id)
                          .eq("version", version).limit(1).execute())


class FormulaireEnqueteurRepository(BaseRepository):
    """public.formulaire_enqueteurs: form <-> field agent assignments."""
    TABLE = "formulaire_enqueteurs"

    def agent_ids(self, formulaire_id: str) -> List[str]:
        """Agents assigned to a form."""
        return [r["agent_id"] for r in self.rows(
            self.query().select("agent_id").eq("formulaire_id", formulaire_id)
            .execute())]

    def formulaire_ids(self, agent_id: str) -> List[str]:
        """Forms assigned to an agent."""
        return [r["formulaire_id"] for r in self.rows(
            self.query().select("formulaire_id").eq("agent_id", agent_id)
            .execute())]

    def is_assigned(self, formulaire_id: str, agent_id: str) -> bool:
        """True if `agent_id` may answer `formulaire_id`."""
        return bool(self.rows(self.query().select("agent_id")
                              .eq("formulaire_id", formulaire_id)
                              .eq("agent_id", agent_id).limit(1).execute()))

    def replace(self, formulaire_id: str, agent_ids: List[str],
                assigned_by: str) -> None:
        """Make `agent_ids` exactly the agents of the form."""
        current = set(self.agent_ids(formulaire_id))
        wanted = set(agent_ids)
        removed = list(current - wanted)
        if removed:
            (self.query().delete().eq("formulaire_id", formulaire_id)
             .in_("agent_id", removed).execute())
        added = wanted - current
        if added:
            self.query().insert([{"formulaire_id": formulaire_id,
                                  "agent_id": a, "assigned_by": assigned_by}
                                 for a in sorted(added)]).execute()
