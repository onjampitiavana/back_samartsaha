-- ============================================================================
-- Smart Saha - fewer round trips per page, batch parcel import, 2D shapes
-- Run once in the Supabase SQL editor, AFTER 20261031_parcels_nom.sql.
-- Idempotent: can be run again without error.
--
-- Flask keeps working before this file is run: it detects the missing
-- functions (PGRST202) and falls back to its parallel queries / one call per
-- imported shape.
--
--   1. insert_parcels()            every shape of an imported file in one call
--                                  and one transaction (all or nothing)
--   2. insert_parcel()             same signature, now stores 2D valid shapes
--   3. get_my_dashboard()          profile + 3 counts           (dashboard)
--   4. get_my_farmers_page()       farmers + cooperatives       (Producteurs)
--   5. get_my_parcels_page()       cooperatives, farmers, rows  (Parcelles)
--   6. get_my_parcel_map(tol)      simplified outlines          (map)
--   7. get_my_data_page()          data management page
--   8. admin_cooperatives_page(a)  an admin's managers + their cooperatives
--
-- 3-7 are SECURITY INVOKER: Row Level Security applies exactly as it does
-- for the queries they replace. 8 reads other members' rows: service key only.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Batch import. KML always carries an altitude ("lon,lat,0") but
--    parcels.geom is 2D: ST_Force2D, or PostGIS refuses the row
--    ("Column has Z dimension but geometry does not").
-- ----------------------------------------------------------------------------
create or replace function public.insert_parcels(
  coop_id      uuid,
  farmer_id_in uuid,
  features     jsonb            -- [{"geometry": <GeoJSON>, "nom": text|null}, ...]
)
returns integer
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  f   jsonb;
  g   geometry;
  i   integer := 0;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;
  if not exists (select 1 from cooperative where uuid = coop_id and owner = auth.uid()) then
    raise exception 'Cooperative not found';
  end if;
  if not exists (select 1 from farmers
                 where uuid = farmer_id_in and owner = auth.uid() and cooperative_id = coop_id) then
    raise exception 'Farmer not found';
  end if;
  if features is null or jsonb_typeof(features) <> 'array' or jsonb_array_length(features) = 0 then
    raise exception 'No parcel to import';
  end if;

  for f in select value from jsonb_array_elements(features) loop
    i := i + 1;
    g := ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON((f -> 'geometry')::text), 4326));
    if not ST_IsValid(g) then
      g := ST_CollectionExtract(ST_MakeValid(g), 3);   -- polygons only
    end if;
    if g is null or ST_IsEmpty(g) or GeometryType(g) not in ('POLYGON', 'MULTIPOLYGON') then
      raise exception 'Shape % is not a polygon', i;
    end if;

    insert into parcels (cooperative_id, farmer_id, owner, nom, geom, area_ha)
    values (coop_id, farmer_id_in, auth.uid(), left(btrim(f ->> 'nom'), 120), g,
            round((ST_Area(g::geography) / 10000)::numeric, 4));
  end loop;

  return i;
end;
$$;

revoke execute on function public.insert_parcels(uuid, uuid, jsonb) from public, anon;
grant execute on function public.insert_parcels(uuid, uuid, jsonb) to authenticated;

-- ----------------------------------------------------------------------------
-- 2. Single-shape import (fallback path, HTML page): same rules as above.
-- ----------------------------------------------------------------------------
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

  g := ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(geom_geojson::text), 4326));
  if not ST_IsValid(g) then
    g := ST_CollectionExtract(ST_MakeValid(g), 3);
  end if;
  if g is null or ST_IsEmpty(g) or GeometryType(g) not in ('POLYGON', 'MULTIPOLYGON') then
    raise exception 'Shape 1 is not a polygon';
  end if;

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
-- 3. Dashboard: the member's profile and record counts.
-- ----------------------------------------------------------------------------
create or replace function public.get_my_dashboard()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'profile', (select to_jsonb(p) from user_profiles p where p.uuid = auth.uid() limit 1),
    'counts', jsonb_build_object(
      'cooperatives', (select count(*) from cooperative),
      'farmers',      (select count(*) from farmers),
      'parcels',      (select count(*) from parcels)
    )
  );
$$;

-- ----------------------------------------------------------------------------
-- 4. Producteurs: farmers (newest first, with their cooperative's name, as
--    `cooperative`: {name}) and the cooperatives (alphabetical).
-- ----------------------------------------------------------------------------
create or replace function public.get_my_farmers_page()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'farmers', coalesce((
      select jsonb_agg(to_jsonb(f) || jsonb_build_object('cooperative', jsonb_build_object('name', c.name))
                       order by f.created_at desc)
      from farmers f
      left join cooperative c on c.uuid = f.cooperative_id), '[]'::jsonb),
    'cooperatives', coalesce((
      select jsonb_agg(jsonb_build_object('uuid', c.uuid, 'name', c.name) order by c.name)
      from cooperative c), '[]'::jsonb)
  );
$$;

-- ----------------------------------------------------------------------------
-- 5. Parcelles: everything but the outlines (see 6).
-- ----------------------------------------------------------------------------
create or replace function public.get_my_parcels_page()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'cooperatives', coalesce((
      select jsonb_agg(jsonb_build_object('uuid', c.uuid, 'name', c.name) order by c.name)
      from cooperative c), '[]'::jsonb),
    'farmers', coalesce((
      select jsonb_agg(jsonb_build_object('uuid', f.uuid, 'full_name', f.full_name,
                                          'cooperative_id', f.cooperative_id)
                       order by f.full_name)
      from farmers f), '[]'::jsonb),
    'parcels', coalesce((
      select jsonb_agg(jsonb_build_object(
               'uuid', p.uuid, 'nom', p.nom, 'cooperative_id', p.cooperative_id,
               'cooperative_name', c.name, 'farmer_id', p.farmer_id,
               'farmer_name', f.full_name, 'area_ha', p.area_ha, 'created_at', p.created_at)
             order by p.created_at desc)
      from parcels p
      left join cooperative c on c.uuid = p.cooperative_id
      left join farmers f     on f.uuid = p.farmer_id
      where p.owner = auth.uid()), '[]'::jsonb)
  );
$$;

-- ----------------------------------------------------------------------------
-- 6. Map outlines, simplified (tolerance in degrees) and rounded to 6
--    decimals: {parcel uuid: GeoJSON geometry}.
-- ----------------------------------------------------------------------------
create or replace function public.get_my_parcel_map(p_tolerance double precision default 0.000005)
returns jsonb
language sql
stable
security invoker
set search_path = public, extensions
as $$
  select coalesce(jsonb_object_agg(
           p.uuid::text,
           ST_AsGeoJSON(case when ST_IsEmpty(s.light) then p.geom else s.light end, 6)::jsonb),
         '{}'::jsonb)
  from parcels p
  cross join lateral (select ST_SimplifyPreserveTopology(p.geom, p_tolerance) as light) s
  where p.owner = auth.uid();
$$;

-- ----------------------------------------------------------------------------
-- 7. Data management page: cooperatives, farmers, parcels and campaign log.
-- ----------------------------------------------------------------------------
create or replace function public.get_my_data_page()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'cooperatives', coalesce((
      select jsonb_agg(jsonb_build_object('uuid', c.uuid, 'name', c.name)) from cooperative c), '[]'::jsonb),
    'farmers', coalesce((
      select jsonb_agg(to_jsonb(f) order by f.full_name) from farmers f), '[]'::jsonb),
    'parcels', coalesce((
      select jsonb_agg(jsonb_build_object(
               'uuid', p.uuid, 'nom', p.nom, 'cooperative_id', p.cooperative_id,
               'farmer_id', p.farmer_id, 'area_ha', p.area_ha,
               'bio_campaign_status', p.bio_campaign_status, 'created_at', p.created_at)
             order by p.created_at desc)
      from parcels p), '[]'::jsonb),
    'campaign_log', coalesce((
      select jsonb_agg(to_jsonb(l)) from public.get_campaign_log() l), '[]'::jsonb)
  );
$$;

revoke execute on function public.get_my_dashboard()        from public, anon;
revoke execute on function public.get_my_farmers_page()     from public, anon;
revoke execute on function public.get_my_parcels_page()     from public, anon;
revoke execute on function public.get_my_parcel_map(double precision) from public, anon;
revoke execute on function public.get_my_data_page()        from public, anon;
grant execute on function public.get_my_dashboard()         to authenticated;
grant execute on function public.get_my_farmers_page()      to authenticated;
grant execute on function public.get_my_parcels_page()      to authenticated;
grant execute on function public.get_my_parcel_map(double precision) to authenticated;
grant execute on function public.get_my_data_page()         to authenticated;

-- ----------------------------------------------------------------------------
-- 8. Cooperatives page of an administrator: his managers and every
--    cooperative he or they own (full rows), in one round trip.
-- ----------------------------------------------------------------------------
create or replace function public.admin_cooperatives_page(p_admin uuid)
returns jsonb
language sql
stable
set search_path = public
as $$
  with managers as (
    select p.uuid, p.username, p.full_name, p.email
    from user_profiles p
    join roles r on r.id = p.role_id and r.role = 'manager'
    where p.admin_id = p_admin
  )
  select jsonb_build_object(
    'managers', coalesce((select jsonb_agg(to_jsonb(m)) from managers m), '[]'::jsonb),
    'cooperatives', coalesce((
      select jsonb_agg(to_jsonb(c) order by c.created_at desc)
      from cooperative c
      where c.owner = p_admin or c.owner in (select uuid from managers)), '[]'::jsonb)
  );
$$;

revoke all on function public.admin_cooperatives_page(uuid) from public, anon, authenticated;
grant execute on function public.admin_cooperatives_page(uuid) to service_role;

notify pgrst, 'reload schema';
