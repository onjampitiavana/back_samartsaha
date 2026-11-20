-- ============================================================================
-- Smart Saha - administrative boundaries (régions, communes) and automatic
-- placement of cooperatives on the dashboard map.
-- Run once in the Supabase SQL editor, AFTER 20261121_dashboard_map.sql.
-- Idempotent: can be run again without error.
--
--   1. admin_regions / admin_communes: reference boundaries (PostGIS), filled
--      by `flask --app run geo-import` from an official boundaries file
--      (e.g. HDX "Madagascar - Subnational Administrative Boundaries",
--      COD-AB: admin1 = régions, admin3 = communes). Read by every signed-in
--      member: the cooperative form's Région / Commune lists.
--   2. admin_geo_upsert(kind, rows): used by the import command (service key).
--   3. geo_locate(lng, lat): région / commune containing a point.
--      cooperative_detect_location(uuid): région / commune of a
--      cooperative, from the centre of its parcels.
--   4. dashboard_member_map: a cooperative is placed at the centre of its
--      parcels; without parcels, at the centre of its commune, else of its
--      région (location_source tells which).
-- ============================================================================

create table if not exists public.admin_regions (
  code  text primary key,
  name  text not null,
  geom  geometry(MultiPolygon, 4326)
);

create table if not exists public.admin_communes (
  code         text primary key,
  name         text not null,
  region_code  text references public.admin_regions(code) on delete set null,
  district     text,
  geom         geometry(MultiPolygon, 4326)
);

create index if not exists admin_regions_geom_idx   on public.admin_regions  using gist (geom);
create index if not exists admin_communes_geom_idx  on public.admin_communes using gist (geom);
create index if not exists admin_communes_region_idx on public.admin_communes (region_code);
create index if not exists admin_regions_name_idx   on public.admin_regions  (lower(name));
create index if not exists admin_communes_name_idx  on public.admin_communes (lower(name));

alter table public.admin_regions  enable row level security;
alter table public.admin_communes enable row level security;

drop policy if exists "Members read regions" on public.admin_regions;
create policy "Members read regions" on public.admin_regions
  for select to authenticated using (true);
drop policy if exists "Members read communes" on public.admin_communes;
create policy "Members read communes" on public.admin_communes
  for select to authenticated using (true);

-- ----------------------------------------------------------------------------
-- 2. Import (service key only)
--    rows: [{code, name, region_code?, district?, geometry: GeoJSON}, ...]
-- ----------------------------------------------------------------------------
create or replace function public.admin_geo_upsert(p_kind text, p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  n integer;
begin
  if p_kind = 'region' then
    insert into admin_regions (code, name, geom)
    select r->>'code', r->>'name',
           ST_Multi(ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(r->>'geometry'), 4326))))
    from jsonb_array_elements(p_rows) r
    on conflict (code) do update set name = excluded.name, geom = excluded.geom;
  elsif p_kind = 'commune' then
    insert into admin_communes (code, name, region_code, district, geom)
    select r->>'code', r->>'name', nullif(r->>'region_code', ''), nullif(r->>'district', ''),
           ST_Multi(ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(r->>'geometry'), 4326))))
    from jsonb_array_elements(p_rows) r
    on conflict (code) do update set name = excluded.name, region_code = excluded.region_code,
                                     district = excluded.district, geom = excluded.geom;
  else
    raise exception 'unknown boundary kind %', p_kind;
  end if;
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function public.admin_geo_upsert(text, jsonb) from public, anon, authenticated;
grant execute on function public.admin_geo_upsert(text, jsonb) to service_role;

-- ----------------------------------------------------------------------------
-- 3. Locating
-- ----------------------------------------------------------------------------
create or replace function public.geo_locate(p_lng double precision, p_lat double precision)
returns jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  with pt as (select ST_SetSRID(ST_MakePoint(p_lng, p_lat), 4326) as g)
  select jsonb_build_object(
    'region', (select r.name from admin_regions r, pt where ST_Intersects(r.geom, pt.g) limit 1),
    'commune', (select c.name from admin_communes c, pt where ST_Intersects(c.geom, pt.g) limit 1));
$$;

grant execute on function public.geo_locate(double precision, double precision) to authenticated;

-- Région / commune of a cooperative from the centre of its parcels. Row
-- Level Security of the caller applies to the parcels read (security invoker).
create or replace function public.cooperative_detect_location(p_cooperative uuid)
returns jsonb
language sql
stable
set search_path = public, extensions
as $$
  with centre as (
    select ST_Centroid(ST_Collect(ST_PointOnSurface(geom))) as g, count(*) as n
    from parcels where cooperative_id = p_cooperative
  )
  select case when centre.n = 0 then jsonb_build_object('parcels', 0)
         else public.geo_locate(ST_X(centre.g), ST_Y(centre.g))
              || jsonb_build_object('parcels', centre.n,
                                    'lat', round(ST_Y(centre.g)::numeric, 6),
                                    'lng', round(ST_X(centre.g)::numeric, 6)) end
  from centre;
$$;

grant execute on function public.cooperative_detect_location(uuid) to authenticated;

-- ----------------------------------------------------------------------------
-- 4. Dashboard: same as 20261121_dashboard_map.sql, plus the placement of
--    cooperatives without parcels and `location_source`.
-- ----------------------------------------------------------------------------
create or replace function public.dashboard_member_map(
  p_uid uuid, p_tolerance double precision default 0.00002)
returns jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  with coops as (
    select c.*
    from cooperative c
    where c.uuid in (select x from public.fire_member_cooperatives(p_uid) x)
  ), parcel_rows as (
    select p.uuid, p.cooperative_id, p.farmer_id, p.nom, p.area_ha,
           p.bio_campaign_status, p.geom, ST_PointOnSurface(p.geom) as pt
    from parcels p
    where p.cooperative_id in (select uuid from coops)
  ), last_audit as (
    select distinct on (a.parcel_id) a.parcel_id, a.overall_status, a.audit_date
    from audits_bio a
    where a.parcel_id in (select uuid from parcel_rows)
    order by a.parcel_id, a.audit_date desc, a.created_at desc
  ), farmer_pos as (
    select farmer_id, ST_Centroid(ST_Collect(pt)) as pt
    from parcel_rows group by farmer_id
  ), coop_parcel_pos as (
    select cooperative_id, ST_Centroid(ST_Collect(pt)) as pt
    from parcel_rows group by cooperative_id
  ), coop_pos as (
    -- 1. centre of its parcels, 2. its commune (in its région when known),
    -- 3. its région.
    select c.uuid as cooperative_id,
           coalesce(pp.pt, com.pt, reg.pt) as pt,
           case when pp.pt is not null then 'parcels'
                when com.pt is not null then 'commune'
                when reg.pt is not null then 'region' end as source
    from coops c
    left join coop_parcel_pos pp on pp.cooperative_id = c.uuid
    left join lateral (
      select ST_PointOnSurface(ac.geom) as pt
      from admin_communes ac
      left join admin_regions ar on ar.code = ac.region_code
      where nullif(btrim(c.commune), '') is not null
        and lower(ac.name) = lower(btrim(c.commune))
        and (nullif(btrim(c.region), '') is null or ar.name is null
             or lower(ar.name) = lower(btrim(c.region)))
      limit 1) com on true
    left join lateral (
      select ST_PointOnSurface(ar.geom) as pt
      from admin_regions ar
      where nullif(btrim(c.region), '') is not null
        and lower(ar.name) = lower(btrim(c.region))
      limit 1) reg on true
  )
  select jsonb_build_object(
    'cooperatives', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', c.uuid,
               'name', c.name,
               'region', nullif(btrim(c.region), ''),
               'commune', nullif(btrim(c.commune), ''),
               'address', c.address,
               'president', c.president_name,
               'status', c.status,
               'crops', case when cardinality(c.crops) > 0 then to_jsonb(c.crops)
                             else to_jsonb(array_remove(array[c.main_crop, c.second_crop, c.third_crop], null)) end,
               'manager', m.full_name,
               'location_source', cp.source,
               'lat', round(ST_Y(cp.pt)::numeric, 6),
               'lng', round(ST_X(cp.pt)::numeric, 6))
             order by c.name)
      from coops c
      left join coop_pos cp on cp.cooperative_id = c.uuid
      left join user_profiles m on m.uuid = c.owner), '[]'::jsonb),
    'farmers', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', f.uuid,
               'cooperative_id', f.cooperative_id,
               'name', f.full_name,
               'gender', f.gender,
               'status', f.status,
               'lat', round(ST_Y(fp.pt)::numeric, 6),
               'lng', round(ST_X(fp.pt)::numeric, 6)))
      from farmers f
      left join farmer_pos fp on fp.farmer_id = f.uuid
      where f.cooperative_id in (select uuid from coops)), '[]'::jsonb),
    'parcels', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', p.uuid,
               'cooperative_id', p.cooperative_id,
               'farmer_id', p.farmer_id,
               'name', p.nom,
               'area_ha', p.area_ha,
               'status', p.bio_campaign_status,
               'audit_status', la.overall_status,
               'audit_date', la.audit_date,
               'lat', round(ST_Y(p.pt)::numeric, 6),
               'lng', round(ST_X(p.pt)::numeric, 6),
               'geometry', ST_AsGeoJSON(case when ST_IsEmpty(s.light) then p.geom else s.light end, 6)::jsonb))
      from parcel_rows p
      left join last_audit la on la.parcel_id = p.uuid
      cross join lateral (select ST_SimplifyPreserveTopology(p.geom, p_tolerance) as light) s),
      '[]'::jsonb)
  );
$$;

revoke all on function public.dashboard_member_map(uuid, double precision) from public, anon, authenticated;
grant execute on function public.dashboard_member_map(uuid, double precision) to service_role;

notify pgrst, 'reload schema';
