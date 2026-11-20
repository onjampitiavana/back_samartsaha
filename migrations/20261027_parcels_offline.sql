-- ============================================================================
-- Smart Saha - parcels traced offline by field agents (mobile app)
-- Run once in the Supabase SQL editor, after 20261015_parcels_agent_gps.sql
-- and 20261026_mobile_offline_sync.sql.
--
-- The app now saves a traced parcel on the phone first (SQLite), with a
-- uuid generated there, and sends it later from its sync queue. Survey
-- responses entered offline may already point at that uuid.
--
-- insert_parcel_by_agent() gets an optional p_uuid:
--   * the parcel is created with that uuid;
--   * if it already exists (an earlier upload whose answer was lost), the
--     existing row is returned instead of creating a duplicate - only if it
--     was traced by the same agent.
-- Called without p_uuid it behaves exactly as before. The 2-argument
-- version is dropped first: two overloads would make the PostgREST call
-- ambiguous.
-- Idempotent: can be run again without error.
-- ============================================================================

drop function if exists public.insert_parcel_by_agent(uuid, jsonb);

create or replace function public.insert_parcel_by_agent(
  farmer_id_in uuid,
  geom_geojson jsonb,
  p_uuid       uuid default null
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
        select p.uuid, p.cooperative_id, p.farmer_id, p.agent_id, p.area_ha,
               p.created_at, ST_AsGeoJSON(p.geom)
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

  insert into parcels (uuid, cooperative_id, farmer_id, owner, agent_id, geom, area_ha)
  values (coalesce(p_uuid, gen_random_uuid()), coop, farmer_id_in, coop_owner,
          auth.uid(), g, round((ST_Area(g::geography) / 10000)::numeric, 4))
  returning parcels.uuid into new_id;

  return query
    select p.uuid, p.cooperative_id, p.farmer_id, p.agent_id, p.area_ha,
           p.created_at, ST_AsGeoJSON(p.geom)
    from parcels p
    where p.uuid = new_id;
end;
$$;

revoke execute on function public.insert_parcel_by_agent(uuid, jsonb, uuid) from public, anon;
grant execute on function public.insert_parcel_by_agent(uuid, jsonb, uuid) to authenticated;

notify pgrst, 'reload schema';
