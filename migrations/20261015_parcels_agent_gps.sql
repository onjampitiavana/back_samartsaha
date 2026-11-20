-- ============================================================================
-- Smart Saha - GPS parcel tracing by field agents (mobile app)
-- Run once in the Supabase SQL editor, after schema.sql and
-- 20261015_user_profiles_cooperative_id.sql.
--
-- Adds parcels.agent_id (the agent who walked the boundary) and two RPCs
-- called by the Flutter app:
--   * get_agent_farmers()                     farmers of the agent's cooperative
--   * insert_parcel_by_agent(farmer, geojson) saves a traced polygon
--
-- The cooperative is ALWAYS read from the agent's own profile
-- (user_profiles.cooperative_id), never taken from the request. The parcel's
-- owner stays the cooperative owner, so the web dashboard (get_my_parcels,
-- RLS "owner = auth.uid()") keeps listing every parcel of the cooperative.
-- Idempotent: can be run again without error.
-- ============================================================================

alter table public.parcels
  add column if not exists agent_id uuid
    references auth.users(id)
    on delete set null;

create index if not exists parcels_agent_idx on public.parcels (agent_id);

-- ----------------------------------------------------------------------------
-- Helper: cooperative of the calling agent, or an exception.
-- ----------------------------------------------------------------------------
create or replace function public.current_agent_cooperative()
returns uuid
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  coop uuid;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;

  select p.cooperative_id into coop
  from user_profiles p
  join roles r on r.id = p.role_id
  where p.uuid = auth.uid() and r.role = 'agent';

  if not found then
    raise exception 'Only field agents can trace parcels';
  end if;
  if coop is null then
    raise exception 'Agent is not attached to a cooperative';
  end if;

  return coop;
end;
$$;

revoke execute on function public.current_agent_cooperative() from public;

-- ----------------------------------------------------------------------------
-- RPC: get_agent_farmers
-- ----------------------------------------------------------------------------
-- Self-contained (doesn't depend on current_agent_cooperative()) so it can
-- also be run on its own. Dropped first: its returned columns changed.
drop function if exists public.get_agent_farmers();

create function public.get_agent_farmers()
returns table (
  uuid          uuid,
  full_name     text,
  gender        text,
  year_of_birth int,
  status        text,
  parcel_count  bigint
)
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  coop uuid;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;

  select p.cooperative_id into coop
  from user_profiles p
  join roles r on r.id = p.role_id
  where p.uuid = auth.uid() and r.role = 'agent';

  if not found then
    raise exception 'Only field agents can list farmers';
  end if;
  if coop is null then
    raise exception 'Agent is not attached to a cooperative';
  end if;

  return query
    select f.uuid, f.full_name, f.gender, f.year_of_birth, f.status,
           (select count(*) from parcels pa where pa.farmer_id = f.uuid)
    from farmers f
    where f.cooperative_id = coop
    order by f.full_name;
end;
$$;

revoke execute on function public.get_agent_farmers() from public, anon;
grant execute on function public.get_agent_farmers() to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: insert_parcel_by_agent
-- geom_geojson is a GeoJSON Polygon (lng/lat, ring closed or not).
-- Area is recomputed here with ST_Area(geography): the app's value is only
-- a live preview.
-- ----------------------------------------------------------------------------
create or replace function public.insert_parcel_by_agent(
  farmer_id_in uuid,
  geom_geojson jsonb
)
returns table (
  uuid           uuid,
  cooperative_id uuid,
  farmer_id      uuid,
  agent_id       uuid,
  area_ha        numeric,
  created_at     timestamptz,
  geojson        text
)
language plpgsql
security definer
set search_path = public
as $$
declare
  coop       uuid := public.current_agent_cooperative();
  coop_owner uuid;
  g          geometry;
  new_id     uuid;
begin
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

  insert into parcels (cooperative_id, farmer_id, owner, agent_id, geom, area_ha)
  values (coop, farmer_id_in, coop_owner, auth.uid(), g,
          round((ST_Area(g::geography) / 10000)::numeric, 4))
  returning parcels.uuid into new_id;

  return query
    select p.uuid, p.cooperative_id, p.farmer_id, p.agent_id, p.area_ha,
           p.created_at, ST_AsGeoJSON(p.geom)
    from parcels p
    where p.uuid = new_id;
end;
$$;

grant execute on function public.insert_parcel_by_agent(uuid, jsonb) to authenticated;

notify pgrst, 'reload schema';
