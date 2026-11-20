-- ============================================================================
-- Smart Saha - interactive map dashboard (Tableau de bord cartographique)
-- Run once in the Supabase SQL editor, after 20261118_fire_alerts.sql
-- (it reuses public.fire_member_cooperatives, the single "who sees what" rule).
--
--   1. cooperative.region / cooperative.commune (optional, typed in the
--      cooperative form): the dashboard's region / commune filters.
--   2. dashboard_member_map(p_uid): every cooperative, farmer and parcel the
--      member may see, in one round trip, scoped by fire_member_cooperatives:
--        admin    his own cooperatives + those of his managers (admin_id)
--        manager  only the cooperatives he owns (cooperative.owner)
--      Positions: a parcel's point is ST_PointOnSurface(geom); a farmer and a
--      cooperative are placed at the centre of their parcels (they have no
--      coordinates of their own). Outlines are simplified for display.
--      Service key only: Flask passes the uid of the verified session, never
--      a value taken from the request.
-- Idempotent: can be run again without error.
-- ============================================================================

alter table public.cooperative add column if not exists region  text;
alter table public.cooperative add column if not exists commune text;

create index if not exists cooperative_owner_idx on public.cooperative (owner);
create index if not exists audits_bio_parcel_date_idx
  on public.audits_bio (parcel_id, audit_date desc, created_at desc);

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
  ), coop_pos as (
    select cooperative_id, ST_Centroid(ST_Collect(pt)) as pt
    from parcel_rows group by cooperative_id
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
