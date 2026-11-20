-- ============================================================================
-- Smart Saha - parcel name (parcels.nom)
-- Run once in the Supabase SQL editor, after 20261027_parcels_offline.sql.
--
-- Every parcel now has a human name, shown everywhere instead of its uuid:
--   * mobile app: the agent types it when validating a traced parcel (saved
--     in SQLite first, sent by insert_parcel_by_agent at the next sync);
--   * web import: taken from the file (name / Name / nom column) if any.
-- A parcel created without a name (old app versions, files without a name
-- column, existing rows) gets "Parcelle XXXXXXXX" (start of its uuid) from
-- a trigger, so the column can stay NOT NULL.
--
-- RPC changes (old overloads dropped first: two overloads would make the
-- PostgREST call ambiguous; the new parameter has a default, so callers
-- that don't send it keep working):
--   * insert_parcel_by_agent(farmer, geojson, p_uuid, p_nom)
--   * insert_parcel(coop, farmer, geojson, nom_in)
--   * get_my_parcels() and get_my_audits() also return the parcel name.
--   * get_my_cooperative_parcels() returns every column: nothing to change.
-- Idempotent: can be run again without error.
-- ============================================================================

alter table public.parcels add column if not exists nom text;

-- ----------------------------------------------------------------------------
-- Default name for a parcel created without one
-- ----------------------------------------------------------------------------
create or replace function public.parcels_default_nom()
returns trigger
language plpgsql
as $$
begin
  new.nom := nullif(btrim(coalesce(new.nom, '')), '');
  if new.nom is null then
    new.nom := 'Parcelle ' || upper(left(new.uuid::text, 8));
  end if;
  return new;
end;
$$;

drop trigger if exists parcels_default_nom on public.parcels;
create trigger parcels_default_nom
  before insert or update of nom on public.parcels
  for each row execute function public.parcels_default_nom();

update public.parcels
set nom = 'Parcelle ' || upper(left(uuid::text, 8))
where nom is null or btrim(nom) = '';

alter table public.parcels alter column nom set not null;

-- ----------------------------------------------------------------------------
-- RPC: insert_parcel_by_agent (replaces the 20261027 version)
-- ----------------------------------------------------------------------------
drop function if exists public.insert_parcel_by_agent(uuid, jsonb);
drop function if exists public.insert_parcel_by_agent(uuid, jsonb, uuid);

create or replace function public.insert_parcel_by_agent(
  farmer_id_in uuid,
  geom_geojson jsonb,
  p_uuid       uuid default null,
  p_nom        text default null
)
returns table (
  uuid           uuid,
  cooperative_id uuid,
  farmer_id      uuid,
  agent_id       uuid,
  nom            text,
  area_ha        numeric,
  created_at     timestamptz,
  geojson        text
)
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  coop       uuid := public.current_agent_cooperative();
  coop_owner uuid;
  g          geometry;
  new_id     uuid;
  existing   uuid;
begin
  if p_uuid is not null then
    select pa.agent_id into existing from parcels pa where pa.uuid = p_uuid;
    if found then
      if existing is distinct from auth.uid() then
        raise exception 'Parcel id already used';
      end if;
      return query
        select p.uuid, p.cooperative_id, p.farmer_id, p.agent_id, p.nom,
               p.area_ha, p.created_at, ST_AsGeoJSON(p.geom)
        from parcels p
        where p.uuid = p_uuid;
      return;
    end if;
  end if;

  if not exists (select 1 from farmers f
                 where f.uuid = farmer_id_in and f.cooperative_id = coop) then
    raise exception 'Farmer not found in your cooperative';
  end if;

  g := ST_SetSRID(ST_GeomFromGeoJSON(geom_geojson::text), 4326);
  if GeometryType(g) <> 'POLYGON' or ST_NPoints(g) < 4 then
    raise exception 'A parcel needs a polygon with at least 3 points';
  end if;
  g := ST_MakeValid(g);

  select c.owner into coop_owner from cooperative c where c.uuid = coop;

  insert into parcels (uuid, cooperative_id, farmer_id, owner, agent_id, nom,
                       geom, area_ha)
  values (coalesce(p_uuid, gen_random_uuid()), coop, farmer_id_in, coop_owner,
          auth.uid(), left(btrim(p_nom), 120), g,
          round((ST_Area(g::geography) / 10000)::numeric, 4))
  returning parcels.uuid into new_id;

  return query
    select p.uuid, p.cooperative_id, p.farmer_id, p.agent_id, p.nom,
           p.area_ha, p.created_at, ST_AsGeoJSON(p.geom)
    from parcels p
    where p.uuid = new_id;
end;
$$;

revoke execute on function public.insert_parcel_by_agent(uuid, jsonb, uuid, text) from public, anon;
grant execute on function public.insert_parcel_by_agent(uuid, jsonb, uuid, text) to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: insert_parcel (web import, replaces the schema.sql version)
-- ----------------------------------------------------------------------------
drop function if exists public.insert_parcel(uuid, uuid, jsonb);

create or replace function public.insert_parcel(
  coop_id uuid,
  farmer_id_in uuid,
  geom_geojson jsonb,
  nom_in text default null
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  new_id uuid;
  g geometry;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;

  if not exists (select 1 from cooperative where uuid = coop_id and owner = auth.uid()) then
    raise exception 'Cooperative not found';
  end if;
  if not exists (select 1 from farmers where uuid = farmer_id_in and owner = auth.uid()) then
    raise exception 'Farmer not found';
  end if;

  g := ST_SetSRID(ST_GeomFromGeoJSON(geom_geojson::text), 4326);

  insert into parcels (cooperative_id, farmer_id, owner, nom, geom, area_ha)
  values (coop_id, farmer_id_in, auth.uid(), left(btrim(nom_in), 120), g,
          round((ST_Area(g::geography) / 10000)::numeric, 4))
  returning uuid into new_id;

  return new_id;
end;
$$;

revoke execute on function public.insert_parcel(uuid, uuid, jsonb, text) from public, anon;
grant execute on function public.insert_parcel(uuid, uuid, jsonb, text) to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: get_my_parcels (returned columns changed: dropped first)
-- ----------------------------------------------------------------------------
drop function if exists public.get_my_parcels();

create function public.get_my_parcels()
returns table (
  uuid              uuid,
  nom               text,
  cooperative_id    uuid,
  cooperative_name  text,
  farmer_id         uuid,
  farmer_name       text,
  area_ha           numeric,
  created_at        timestamptz,
  geojson           text
)
language sql
security definer
set search_path = public, extensions
as $$
  select
    p.uuid, p.nom, p.cooperative_id, c.name, p.farmer_id, f.full_name,
    p.area_ha, p.created_at, ST_AsGeoJSON(p.geom)
  from parcels p
  join cooperative c on c.uuid = p.cooperative_id
  join farmers f       on f.uuid = p.farmer_id
  where p.owner = auth.uid()
  order by p.created_at desc;
$$;

grant execute on function public.get_my_parcels() to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: get_my_audits (returned columns changed: dropped first)
-- ----------------------------------------------------------------------------
drop function if exists public.get_my_audits();

create function public.get_my_audits()
returns table (
  uuid                      uuid,
  parcel_id                 uuid,
  parcel_name               text,
  cooperative_name          text,
  farmer_name               text,
  area_ha                   numeric,
  audit_date                date,
  auditor_name              text,
  conversion_status         text,
  contamination_risk        text,
  non_conformity_severity   text,
  overall_status            text,
  created_at                timestamptz
)
language sql
security definer
set search_path = public
as $$
  select
    a.uuid, a.parcel_id, p.nom, c.name, f.full_name, p.area_ha,
    a.audit_date, a.auditor_name, a.conversion_status, a.contamination_risk,
    a.non_conformity_severity, a.overall_status, a.created_at
  from audits_bio a
  join parcels p     on p.uuid = a.parcel_id
  join cooperative c on c.uuid = p.cooperative_id
  join farmers f     on f.uuid = p.farmer_id
  where a.owner = auth.uid()
  order by a.created_at desc;
$$;

grant execute on function public.get_my_audits() to authenticated;

notify pgrst, 'reload schema';
