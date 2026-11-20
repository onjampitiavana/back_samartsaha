-- ============================================================================
-- Smart Saha - 4.3 Carte de suivi des feux et alertes de proximité
-- Run once in the Supabase SQL editor, after 20261116_performance.sql.
--
-- NASA FIRMS hotspots are fetched by the Flask server only (MAP_KEY never
-- leaves it), at most once per refresh interval, for the bounding box of the
-- registered parcels. Every client (Nuxt via Flask, Flutter via the RPCs at
-- the end) reads them from these tables.
--
--   fire_hotspots     cache of the detected hotspots (deduplicated)
--   fire_alerts       one row per (hotspot, parcel) within the alert radius:
--                     the unique constraint is what prevents duplicate alerts
--   fire_fetch_log    one row per FIRMS refresh (also the cross-worker lock)
--   fire_alert_reads  last alert each member has seen (notification badge)
--
-- Who sees what (fire_member_cooperatives): superadmin every cooperative,
-- admin his own + his managers', manager the cooperatives he owns, agent /
-- auditor / consultant the cooperative of his profile.
-- Idempotent: can be run again without error.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Tables
-- ----------------------------------------------------------------------------
create table if not exists public.fire_hotspots (
  id          bigint generated always as identity primary key,
  source      text not null,                       -- VIIRS_SNPP_NRT, MODIS_NRT, ...
  latitude    double precision not null,
  longitude   double precision not null,
  geom        geometry(Point, 4326) not null,
  acq_at      timestamptz not null,                -- acquisition (UTC)
  confidence  text,                                -- l / n / h (MODIS % mapped)
  frp         real,                                -- fire radiative power (MW)
  brightness  real,
  daynight    char(1),
  fetched_at  timestamptz not null default now(),
  unique (source, latitude, longitude, acq_at)
);

create index if not exists fire_hotspots_geom_gix on public.fire_hotspots using gist (geom);
create index if not exists fire_hotspots_acq_idx  on public.fire_hotspots (acq_at desc);

create table if not exists public.fire_alerts (
  id              bigint generated always as identity primary key,
  hotspot_id      bigint not null references public.fire_hotspots(id) on delete cascade,
  parcel_id       uuid   not null references public.parcels(uuid) on delete cascade,
  cooperative_id  uuid   not null references public.cooperative(uuid) on delete cascade,
  distance_m      integer not null,
  created_at      timestamptz not null default now(),
  emailed_at      timestamptz,
  unique (hotspot_id, parcel_id)
);

create index if not exists fire_alerts_coop_idx    on public.fire_alerts (cooperative_id, id desc);
create index if not exists fire_alerts_parcel_idx  on public.fire_alerts (parcel_id);
create index if not exists fire_alerts_unsent_idx  on public.fire_alerts (id) where emailed_at is null;

create table if not exists public.fire_fetch_log (
  id           bigint generated always as identity primary key,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  status       text not null default 'running' check (status in ('running', 'ok', 'failed')),
  bbox         text,
  sources      text,
  day_range    int,
  received     int,
  inserted     int,
  alerts       int,
  error        text
);

create index if not exists fire_fetch_log_started_idx on public.fire_fetch_log (started_at desc);

create table if not exists public.fire_alert_reads (
  user_id       uuid primary key references auth.users(id) on delete cascade,
  last_read_id  bigint not null default 0,
  updated_at    timestamptz not null default now()
);

-- No policy: only the service role and the SECURITY DEFINER functions below
-- read or write these tables.
alter table public.fire_hotspots    enable row level security;
alter table public.fire_alerts      enable row level security;
alter table public.fire_fetch_log   enable row level security;
alter table public.fire_alert_reads enable row level security;

-- ----------------------------------------------------------------------------
-- Visibility: the cooperatives whose parcels / alerts p_uid may see.
-- ----------------------------------------------------------------------------
create or replace function public.fire_member_cooperatives(p_uid uuid)
returns setof uuid
language sql
stable
security definer
set search_path = public
as $$
  with me as (
    select r.role, p.cooperative_id
    from user_profiles p
    join roles r on r.id = p.role_id
    where p.uuid = p_uid
  )
  select c.uuid
  from cooperative c, me
  where me.role = 'superadmin'
     or (me.role = 'admin' and (c.owner = p_uid or c.created_by = p_uid
          or c.owner in (select u.uuid from user_profiles u where u.admin_id = p_uid)))
     or (me.role = 'manager' and c.owner = p_uid)
     or (me.role in ('agent', 'auditor', 'consultant') and c.uuid = me.cooperative_id);
$$;

-- ----------------------------------------------------------------------------
-- Server side (Flask, service role): fetch lock, area, ingestion, e-mails.
-- ----------------------------------------------------------------------------

-- Extent of every registered parcel, widened by p_margin_deg, or null.
create or replace function public.fire_parcels_extent(p_margin_deg double precision default 0.1)
returns jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  select case when e is null then null else jsonb_build_object(
           'west',  greatest(-180, ST_XMin(e) - p_margin_deg),
           'south', greatest(-90,  ST_YMin(e) - p_margin_deg),
           'east',  least(180,     ST_XMax(e) + p_margin_deg),
           'north', least(90,      ST_YMax(e) + p_margin_deg)) end
  from (select ST_Extent(geom)::box3d as e from parcels) x;
$$;

-- Claim the next FIRMS refresh. Returns {id, last_ok} or null when another
-- worker is fetching, or the last successful fetch is younger than
-- p_interval_minutes (a failed one blocks retries for 10 minutes).
create or replace function public.fire_claim_fetch(p_interval_minutes int, p_force boolean default false)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_last_ok timestamptz;
  v_id      bigint;
begin
  lock table fire_fetch_log in share row exclusive mode;

  if exists (select 1 from fire_fetch_log
             where status = 'running' and started_at > now() - interval '30 minutes') then
    return null;
  end if;

  select max(started_at) into v_last_ok from fire_fetch_log where status = 'ok';

  if not p_force then
    if v_last_ok is not null and v_last_ok > now() - make_interval(mins => p_interval_minutes) then
      return null;
    end if;
    if exists (select 1 from fire_fetch_log
               where status = 'failed' and started_at > now() - interval '10 minutes') then
      return null;
    end if;
  end if;

  insert into fire_fetch_log default values returning id into v_id;
  return jsonb_build_object('id', v_id, 'last_ok', v_last_ok);
end;
$$;

create or replace function public.fire_finish_fetch(
  p_id bigint, p_status text, p_bbox text, p_sources text, p_day_range int,
  p_received int, p_inserted int, p_alerts int, p_error text)
returns void
language sql
volatile
security definer
set search_path = public
as $$
  update fire_fetch_log
  set finished_at = now(), status = p_status, bbox = p_bbox, sources = p_sources,
      day_range = p_day_range, received = p_received, inserted = p_inserted,
      alerts = p_alerts, error = left(p_error, 500)
  where id = p_id;
  delete from fire_fetch_log where started_at < now() - interval '60 days';
$$;

-- Store FIRMS rows [{source, lat, lng, t, conf, frp, bright, dn}] (duplicates
-- ignored), then create the alerts of every hotspot of the last 48 hours
-- lying within p_radius_m of a parcel. Alerts already recorded for a
-- (hotspot, parcel) pair are never created twice. Hotspots older than 35
-- days that raised no alert are purged.
create or replace function public.fire_ingest(p_rows jsonb, p_radius_m int default 5000)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public, extensions
as $$
declare
  v_inserted int := 0;
  v_alerts   int := 0;
begin
  with incoming as (
    select r ->> 'source'            as source,
           (r ->> 'lat')::float8     as lat,
           (r ->> 'lng')::float8     as lng,
           (r ->> 't')::timestamptz  as acq_at,
           r ->> 'conf'              as conf,
           (r ->> 'frp')::real       as frp,
           (r ->> 'bright')::real    as bright,
           left(r ->> 'dn', 1)       as dn
    from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) r
  ), ins as (
    insert into fire_hotspots (source, latitude, longitude, geom, acq_at, confidence,
                               frp, brightness, daynight)
    select source, lat, lng, ST_SetSRID(ST_MakePoint(lng, lat), 4326), acq_at, conf,
           frp, bright, dn
    from incoming
    where lat between -90 and 90 and lng between -180 and 180 and acq_at is not null
    on conflict (source, latitude, longitude, acq_at) do nothing
    returning id
  )
  select count(*) into v_inserted from ins;

  with recent as (
    select h.id, h.geom, h.latitude
    from fire_hotspots h
    where h.acq_at > now() - interval '48 hours'
  ), created as (
    insert into fire_alerts (hotspot_id, parcel_id, cooperative_id, distance_m)
    select h.id, p.uuid, p.cooperative_id,
           round(ST_Distance(h.geom::geography, p.geom::geography))::int
    from recent h
    join parcels p
      -- bounding-box prefilter (uses parcels_geom_gix), then exact distance
      on p.geom && ST_Expand(h.geom, p_radius_m / 111320.0
                                     / greatest(cos(radians(h.latitude)), 0.2))
     and ST_DWithin(h.geom::geography, p.geom::geography, p_radius_m)
    on conflict (hotspot_id, parcel_id) do nothing
    returning id
  )
  select count(*) into v_alerts from created;

  delete from fire_hotspots h
  where h.acq_at < now() - interval '35 days'
    and not exists (select 1 from fire_alerts a where a.hotspot_id = h.id);

  return jsonb_build_object('inserted', v_inserted, 'alerts', v_alerts);
end;
$$;

-- Alerts not e-mailed yet (oldest first) with what the message needs, and
-- who must receive them: the cooperative's manager, his admin, the admin
-- who created it, and its staff (agent, auditor, consultant).
create or replace function public.fire_pending_email_alerts(p_limit int default 500)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  with pending as (
    select a.id, a.cooperative_id, a.distance_m, p.nom, f.full_name as farmer_name,
           c.name as cooperative_name, h.acq_at, h.latitude, h.longitude, h.confidence, h.frp
    from fire_alerts a
    join fire_hotspots h on h.id = a.hotspot_id
    join parcels p       on p.uuid = a.parcel_id
    join cooperative c   on c.uuid = a.cooperative_id
    left join farmers f  on f.uuid = p.farmer_id
    where a.emailed_at is null
    order by a.id
    limit p_limit
  ), coops as (
    select distinct cooperative_id from pending
  ), recipients as (
    select c.uuid as cooperative_id, u.email, u.full_name
    from cooperative c join user_profiles u on u.uuid = c.owner
    where c.uuid in (select cooperative_id from coops)
    union
    select c.uuid, a.email, a.full_name
    from cooperative c
    join user_profiles m on m.uuid = c.owner
    join user_profiles a on a.uuid = m.admin_id
    where c.uuid in (select cooperative_id from coops)
    union
    select c.uuid, u.email, u.full_name
    from cooperative c join user_profiles u on u.uuid = c.created_by
    where c.uuid in (select cooperative_id from coops)
    union
    select u.cooperative_id, u.email, u.full_name
    from user_profiles u
    join roles r on r.id = u.role_id and r.role in ('agent', 'auditor', 'consultant')
    where u.cooperative_id in (select cooperative_id from coops)
  )
  select jsonb_build_object(
    'alerts', coalesce((select jsonb_agg(to_jsonb(pending) order by id) from pending), '[]'::jsonb),
    'recipients', coalesce((select jsonb_agg(to_jsonb(r)) from recipients r
                            where r.email is not null and r.email <> ''), '[]'::jsonb));
$$;

create or replace function public.fire_mark_emailed(p_ids bigint[])
returns void
language sql
volatile
security definer
set search_path = public
as $$
  update fire_alerts set emailed_at = now() where id = any(p_ids) and emailed_at is null;
$$;

-- ----------------------------------------------------------------------------
-- Reading (Flask on behalf of p_uid, after checking the session).
-- Compact JSON: hotspots as [id, lat, lng, epoch s, confidence, frp].
-- ----------------------------------------------------------------------------
create or replace function public.fire_member_map(
  p_uid uuid, p_since timestamptz, p_margin_deg double precision default 0.1)
returns jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  with coops as (
    select c as uuid from public.fire_member_cooperatives(p_uid) c
  ), visible as (
    select p.uuid, p.geom from parcels p where p.cooperative_id in (select uuid from coops)
  ), area as (
    select ST_SetSRID(ST_Expand(ST_Extent(geom), p_margin_deg)::geometry, 4326) as e from visible
  ), hs as (
    select h.id, h.latitude, h.longitude, h.acq_at, h.confidence, h.frp
    from fire_hotspots h, area
    where area.e is not null and h.geom && area.e and h.acq_at >= p_since
  ), risk as (
    select a.parcel_id, min(a.distance_m) as d, count(*) as n
    from fire_alerts a
    join fire_hotspots h on h.id = a.hotspot_id
    where a.cooperative_id in (select uuid from coops) and h.acq_at >= p_since
    group by a.parcel_id
  )
  select jsonb_build_object(
    'hotspots', coalesce((select jsonb_agg(jsonb_build_array(
                   id, round(latitude::numeric, 4), round(longitude::numeric, 4),
                   extract(epoch from acq_at)::bigint, confidence, round(frp::numeric, 1))
                   order by acq_at desc) from hs), '[]'::jsonb),
    'at_risk', coalesce((select jsonb_object_agg(parcel_id::text, jsonb_build_array(d, n))
                         from risk), '{}'::jsonb),
    'parcels_version', (select left(md5(coalesce(string_agg(uuid::text, ',' order by uuid), '')), 16)
                        from visible),
    'parcel_count', (select count(*) from visible),
    'last_fetch', (select max(finished_at) from fire_fetch_log where status = 'ok'));
$$;

-- Simplified outlines of the member's parcels {uuid: [nom, farmer, geojson]}.
create or replace function public.fire_member_parcels(
  p_uid uuid, p_tolerance double precision default 0.00001)
returns jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  select coalesce(jsonb_object_agg(p.uuid::text, jsonb_build_array(
           p.nom, f.full_name,
           ST_AsGeoJSON(case when ST_IsEmpty(s.light) then p.geom else s.light end, 6)::jsonb)),
         '{}'::jsonb)
  from parcels p
  left join farmers f on f.uuid = p.farmer_id
  cross join lateral (select ST_SimplifyPreserveTopology(p.geom, p_tolerance) as light) s
  where p.cooperative_id in (select c from public.fire_member_cooperatives(p_uid) c);
$$;

-- Alert log, newest first, `p_before_id` for the next page.
create or replace function public.fire_member_alerts(
  p_uid uuid, p_limit int default 50, p_before_id bigint default null)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(jsonb_agg(x order by x.id desc), '[]'::jsonb)
  from (
    select a.id, a.created_at, a.distance_m, a.parcel_id, p.nom, f.full_name as farmer_name,
           c.name as cooperative_name, h.acq_at, round(h.latitude::numeric, 4) as lat,
           round(h.longitude::numeric, 4) as lng, h.confidence, h.frp,
           a.emailed_at is not null as emailed
    from fire_alerts a
    join fire_hotspots h on h.id = a.hotspot_id
    join parcels p       on p.uuid = a.parcel_id
    join cooperative c   on c.uuid = a.cooperative_id
    left join farmers f  on f.uuid = p.farmer_id
    where a.cooperative_id in (select c2 from public.fire_member_cooperatives(p_uid) c2)
      and (p_before_id is null or a.id < p_before_id)
    order by a.id desc
    limit least(greatest(p_limit, 1), 200)
  ) x;
$$;

-- Notification badge: {count, last_read_id, latest: 5 newest unread}.
create or replace function public.fire_member_unread(p_uid uuid)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  with seen as (
    select coalesce((select last_read_id from fire_alert_reads where user_id = p_uid), 0) as id
  ), unread as (
    select a.id, a.distance_m, p.nom, f.full_name as farmer_name, h.acq_at
    from fire_alerts a
    join fire_hotspots h on h.id = a.hotspot_id
    join parcels p       on p.uuid = a.parcel_id
    left join farmers f  on f.uuid = p.farmer_id
    where a.cooperative_id in (select c from public.fire_member_cooperatives(p_uid) c)
      and a.id > (select id from seen)
  )
  select jsonb_build_object(
    'count', (select count(*) from unread),
    'last_read_id', (select id from seen),
    'latest', coalesce((select jsonb_agg(to_jsonb(u) order by u.id desc)
                        from (select * from unread order by id desc limit 5) u), '[]'::jsonb));
$$;

create or replace function public.fire_mark_read(p_uid uuid, p_last_id bigint)
returns bigint
language sql
volatile
security definer
set search_path = public
as $$
  insert into fire_alert_reads (user_id, last_read_id, updated_at)
  values (p_uid, greatest(coalesce(p_last_id, 0), 0), now())
  on conflict (user_id) do update
    set last_read_id = greatest(fire_alert_reads.last_read_id, excluded.last_read_id),
        updated_at = now()
  returning last_read_id;
$$;

-- These take any uid: server only.
revoke all on function public.fire_member_cooperatives(uuid) from public, anon, authenticated;
revoke all on function public.fire_parcels_extent(double precision) from public, anon, authenticated;
revoke all on function public.fire_claim_fetch(int, boolean) from public, anon, authenticated;
revoke all on function public.fire_finish_fetch(bigint, text, text, text, int, int, int, int, text) from public, anon, authenticated;
revoke all on function public.fire_ingest(jsonb, int) from public, anon, authenticated;
revoke all on function public.fire_pending_email_alerts(int) from public, anon, authenticated;
revoke all on function public.fire_mark_emailed(bigint[]) from public, anon, authenticated;
revoke all on function public.fire_member_map(uuid, timestamptz, double precision) from public, anon, authenticated;
revoke all on function public.fire_member_parcels(uuid, double precision) from public, anon, authenticated;
revoke all on function public.fire_member_alerts(uuid, int, bigint) from public, anon, authenticated;
revoke all on function public.fire_member_unread(uuid) from public, anon, authenticated;
revoke all on function public.fire_mark_read(uuid, bigint) from public, anon, authenticated;
grant execute on function public.fire_member_cooperatives(uuid) to service_role;
grant execute on function public.fire_parcels_extent(double precision) to service_role;
grant execute on function public.fire_claim_fetch(int, boolean) to service_role;
grant execute on function public.fire_finish_fetch(bigint, text, text, text, int, int, int, int, text) to service_role;
grant execute on function public.fire_ingest(jsonb, int) to service_role;
grant execute on function public.fire_pending_email_alerts(int) to service_role;
grant execute on function public.fire_mark_emailed(bigint[]) to service_role;
grant execute on function public.fire_member_map(uuid, timestamptz, double precision) to service_role;
grant execute on function public.fire_member_parcels(uuid, double precision) to service_role;
grant execute on function public.fire_member_alerts(uuid, int, bigint) to service_role;
grant execute on function public.fire_member_unread(uuid) to service_role;
grant execute on function public.fire_mark_read(uuid, bigint) to service_role;

-- ----------------------------------------------------------------------------
-- Mobile app (authenticated, always the caller's own data): delta sync.
--
-- get_my_fire_updates(since_hotspot, since_alert, days) returns only the
-- rows the phone does not have yet (ids above its high-water marks):
--   hotspots  [id, lat, lng, epoch s, confidence, frp]       near its parcels
--   alerts    [id, hotspot_id, parcel_id, distance_m, epoch s created]
--   max_hotspot_id / max_alert_id (new high-water marks), last_read_id
-- ----------------------------------------------------------------------------
create or replace function public.get_my_fire_updates(
  p_since_hotspot bigint default 0, p_since_alert bigint default 0, p_days int default 30)
returns jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  with coops as (
    select c as uuid from public.fire_member_cooperatives(auth.uid()) c
  ), area as (
    select ST_SetSRID(ST_Expand(ST_Extent(p.geom), 0.1)::geometry, 4326) as e
    from parcels p where p.cooperative_id in (select uuid from coops)
  ), hs as (
    select h.* from fire_hotspots h, area
    where area.e is not null and h.geom && area.e
      and h.id > coalesce(p_since_hotspot, 0)
      and h.acq_at > now() - make_interval(days => least(greatest(p_days, 1), 30))
  ), al as (
    select a.* from fire_alerts a
    where a.cooperative_id in (select uuid from coops) and a.id > coalesce(p_since_alert, 0)
  )
  select jsonb_build_object(
    'hotspots', coalesce((select jsonb_agg(jsonb_build_array(
                   id, round(latitude::numeric, 4), round(longitude::numeric, 4),
                   extract(epoch from acq_at)::bigint, confidence, round(frp::numeric, 1)))
                   from hs), '[]'::jsonb),
    'alerts', coalesce((select jsonb_agg(jsonb_build_array(
                 id, hotspot_id, parcel_id, distance_m, extract(epoch from created_at)::bigint))
                 from al), '[]'::jsonb),
    'max_hotspot_id', greatest(coalesce(p_since_hotspot, 0), coalesce((select max(id) from hs), 0)),
    'max_alert_id', greatest(coalesce(p_since_alert, 0), coalesce((select max(id) from al), 0)),
    'last_read_id', coalesce((select last_read_id from fire_alert_reads
                              where user_id = auth.uid()), 0));
$$;

create or replace function public.mark_my_fire_alerts_read(p_last_id bigint)
returns bigint
language sql
volatile
security definer
set search_path = public
as $$
  select public.fire_mark_read(auth.uid(), p_last_id) where auth.uid() is not null;
$$;

revoke execute on function public.get_my_fire_updates(bigint, bigint, int) from public, anon;
revoke execute on function public.mark_my_fire_alerts_read(bigint) from public, anon;
grant execute on function public.get_my_fire_updates(bigint, bigint, int) to authenticated;
grant execute on function public.mark_my_fire_alerts_read(bigint) to authenticated;

notify pgrst, 'reload schema';
