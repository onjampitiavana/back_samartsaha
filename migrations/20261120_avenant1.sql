-- ============================================================================
-- Smart Saha - Avenant n°1
-- Run once in the Supabase SQL editor, after 20261118_fire_alerts.sql
-- (uses fire_member_cooperatives) and 20261119_parcels_drawn.sql.
--
--   Lot A/B  KoboToolbox: kobo_connections (encrypted personal token),
--            kobo_forms (one row per imported Kobo form), one table per form
--            in the private schema kobo_data, kobo_import_logs.
--   Lot C    Kobo geometry (geopoint / geotrace / geoshape) converted by
--            kobo_parse_geo() into PostGIS (EPSG:4326) on every write.
--   Lot D    Survey forms: new question type `geoshape` (polygon drawn on
--            the map), stored as geometry(Polygon, 4326).
--   Lot E    fiche_modeles (parcel sheet templates) + fiche_parcel_data().
--
-- Every new table has Row Level Security enabled. Kobo tables and the
-- dynamic ones are reachable by the service role only: Flask checks the
-- member's role (admin / manager) before every call, exactly like the
-- survey forms (schema form_data).
-- Idempotent: can be run again without error.
-- ============================================================================

-- ============================================================================
-- Lot D - `geoshape` question type
-- ============================================================================
create or replace function public.formulaire_column_sql(f jsonb)
returns text
language plpgsql
immutable
set search_path = public, extensions, pg_temp
as $$
declare
  k        text := f->>'key';
  t        text := f->>'type';
  req      boolean := coalesce((f->>'required')::boolean, false);
  col_type text;
  checks   text := '';
  opts     text[];
  lo       int;
  hi       int;
begin
  if k is null or k !~ '^[a-z][a-z0-9_]{0,47}$' then
    raise exception 'Invalid field key: %', coalesce(k, '<null>');
  end if;
  if k in ('id', 'enqueteur_id', 'farmer_id', 'parcel_id', 'submitted_at') then
    raise exception 'Reserved field key: %', k;
  end if;

  case t
    when 'text_short', 'text_long', 'photo', 'signature' then col_type := 'text';
    when 'integer'  then col_type := 'bigint';
    when 'decimal'  then col_type := 'numeric';
    when 'date'     then col_type := 'date';
    when 'time'     then col_type := 'time';
    when 'datetime' then col_type := 'timestamptz';
    when 'geolocation' then col_type := 'geometry(Point, 4326)';
    when 'geoshape'    then col_type := 'geometry(Polygon, 4326)';
    when 'single_choice', 'multiple_choice' then
      select array_agg(o) into opts
      from jsonb_array_elements_text(coalesce(f->'options', '[]'::jsonb)) o;
      if opts is null or array_length(opts, 1) < 2 then
        raise exception 'Field % needs at least two options', k;
      end if;
      if t = 'single_choice' then
        col_type := 'text';
        checks := format(' check (%I = any (%L::text[]))', k, opts);
      else
        col_type := 'text[]';
        checks := format(' check (%I <@ %L::text[])', k, opts);
      end if;
    when 'scale' then
      lo := (f->>'min')::int;
      hi := (f->>'max')::int;
      if lo is null or hi is null or lo >= hi or hi - lo > 100 then
        raise exception 'Field % has an invalid scale range', k;
      end if;
      col_type := 'integer';
      checks := format(' check (%I between %s and %s)', k, lo, hi);
    else
      raise exception 'Unknown field type: %', coalesce(t, '<null>');
  end case;

  return format('%I %s%s%s', k, col_type, case when req then ' not null' else '' end, checks);
end;
$$;

revoke execute on function public.formulaire_column_sql(jsonb) from public, anon, authenticated;

-- Same as 20261025, with geoshape answers also returned as GeoJSON.
create or replace function public.get_formulaire_responses(
  p_formulaire_id uuid,
  p_version       int,
  p_enqueteur_id  uuid default null,
  p_limit         int  default 500
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  ver    public.formulaire_versions;
  cols   text;
  result jsonb;
begin
  select v.* into ver from public.formulaire_versions v
  where v.formulaire_id = p_formulaire_id and v.version = p_version;
  if not found then
    raise exception 'Form version not found';
  end if;

  select string_agg(
           case when f->>'type' in ('geolocation', 'geoshape')
                then format('ST_AsGeoJSON(r.%1$I, 7)::jsonb as %1$I', f->>'key')
                else format('r.%I', f->>'key') end, ', ')
    into cols
  from jsonb_array_elements(ver.fields) f;

  execute format(
    'select coalesce(jsonb_agg(to_jsonb(x) order by x.submitted_at desc), ''[]''::jsonb)
     from (select r.id, r.enqueteur_id, r.farmer_id, r.parcel_id, r.submitted_at,
                  p.full_name as enqueteur_name, fa.full_name as farmer_name, %s
           from form_data.%I r
           left join public.user_profiles p on p.uuid = r.enqueteur_id
           left join public.farmers fa on fa.uuid = r.farmer_id
           where $1 is null or r.enqueteur_id = $1
           order by r.submitted_at desc
           limit $2) x', cols, ver.table_name)
  into result
  using p_enqueteur_id, greatest(1, least(coalesce(p_limit, 500), 5000));

  return result;
end;
$$;

revoke execute on function public.get_formulaire_responses(uuid, int, uuid, int) from public, anon, authenticated;
grant execute on function public.get_formulaire_responses(uuid, int, uuid, int) to service_role;

-- ============================================================================
-- Lot A/B/C - KoboToolbox
-- ============================================================================
create table if not exists public.kobo_connections (
  user_id          uuid primary key references auth.users(id) on delete cascade,
  server_url       text not null,
  token_encrypted  text not null,           -- Fernet (AES-128-CBC + HMAC), key on the server only
  token_hint       text,                    -- last 4 characters, for display
  kobo_username    text,
  verified_at      timestamptz,
  updated_at       timestamptz not null default now()
);

create table if not exists public.kobo_forms (
  id                uuid primary key default gen_random_uuid(),
  owner             uuid not null references auth.users(id) on delete cascade,
  cooperative_id    uuid references public.cooperative(uuid) on delete set null,
  asset_uid         text not null check (asset_uid ~ '^[A-Za-z0-9]{8,40}$'),
  name              text not null,
  columns           jsonb not null default '[]'::jsonb,   -- [{key, label, type}]
  geo_column        text,
  geo_kind          text check (geo_kind in ('point', 'line', 'polygon')),
  geo_detection     text not null default 'none'
                    check (geo_detection in ('auto', 'uncertain', 'manual', 'none')),
  table_name        text not null unique,
  last_kobo_id      bigint not null default 0,            -- delta sync high-water mark
  record_count      int not null default 0,
  last_sync_at      timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (owner, asset_uid)
);

create index if not exists kobo_forms_coop_idx on public.kobo_forms (cooperative_id);

create table if not exists public.kobo_import_logs (
  id            bigint generated always as identity primary key,
  kobo_form_id  uuid not null references public.kobo_forms(id) on delete cascade,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  status        text not null default 'running' check (status in ('running', 'ok', 'failed')),
  received      int not null default 0,
  inserted      int not null default 0,
  duplicates    int not null default 0,
  error         text,
  triggered_by  uuid references auth.users(id) on delete set null
);

create index if not exists kobo_import_logs_form_idx on public.kobo_import_logs (kobo_form_id, id desc);

alter table public.kobo_connections enable row level security;
alter table public.kobo_forms       enable row level security;
alter table public.kobo_import_logs enable row level security;
revoke all on public.kobo_connections from anon, authenticated;
revoke all on public.kobo_forms       from anon, authenticated;
revoke all on public.kobo_import_logs from anon, authenticated;

create schema if not exists kobo_data;
revoke all on schema kobo_data from public, anon, authenticated;
grant usage on schema kobo_data to service_role;

-- Kobo geo answer -> PostGIS. Kobo writes "lat lng alt acc" per vertex,
-- vertices separated by ";". Anything unreadable gives null (the record is
-- kept, only without geometry).
create or replace function public.kobo_parse_geo(p_value text, p_kind text)
returns geometry
language plpgsql
immutable
set search_path = public, extensions
as $$
declare
  part  text;
  xy    text[];
  pts   geometry[] := '{}';
  g     geometry;
begin
  if p_value is null or btrim(p_value) = '' or p_kind is null then
    return null;
  end if;
  foreach part in array string_to_array(btrim(p_value, ' ;'), ';') loop
    xy := regexp_split_to_array(btrim(part), '\s+');
    if array_length(xy, 1) < 2 then
      return null;
    end if;
    begin
      if abs(xy[1]::float8) > 90 or abs(xy[2]::float8) > 180 then
        return null;
      end if;
      pts := pts || ST_SetSRID(ST_MakePoint(xy[2]::float8, xy[1]::float8), 4326);
    exception when others then
      return null;
    end;
  end loop;

  if p_kind = 'point' then
    return pts[1];
  elsif p_kind = 'line' then
    if array_length(pts, 1) < 2 then return null; end if;
    return ST_MakeLine(pts);
  elsif p_kind = 'polygon' then
    if array_length(pts, 1) < 3 then return null; end if;
    if not ST_Equals(pts[1], pts[array_length(pts, 1)]) then
      pts := pts || pts[1];
    end if;
    if array_length(pts, 1) < 4 then return null; end if;
    g := ST_MakePolygon(ST_MakeLine(pts));
    if not ST_IsValid(g) then
      g := ST_CollectionExtract(ST_MakeValid(g), 3);
    end if;
    return case when g is null or ST_IsEmpty(g) then null else g end;
  end if;
  return null;
end;
$$;

-- Creates the table of an imported Kobo form (kobo_forms row already there).
create or replace function public.kobo_create_table(p_form_id uuid)
returns text
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  tbl text;
begin
  select table_name into tbl from kobo_forms where id = p_form_id;
  if tbl is null or tbl !~ '^k_[a-f0-9]{32}$' then
    raise exception 'Kobo form not found';
  end if;
  execute format(
    'create table if not exists kobo_data.%I (
       id            uuid primary key default gen_random_uuid(),
       kobo_id       bigint unique,          -- Kobo _id: null for records added here
       kobo_uuid     text,
       submitted_at  timestamptz,
       data          jsonb not null default ''{}''::jsonb,
       geom          geometry(Geometry, 4326),
       source        text not null default ''kobo'' check (source in (''kobo'', ''manual'')),
       edited_at     timestamptz,
       created_at    timestamptz not null default now())', tbl);
  execute format('create index if not exists %I on kobo_data.%I using gist (geom)', tbl || '_geom', tbl);
  execute format('create index if not exists %I on kobo_data.%I (submitted_at desc)', tbl || '_sub', tbl);
  execute format('alter table kobo_data.%I enable row level security', tbl);
  execute format('revoke all on kobo_data.%I from public, anon, authenticated', tbl);
  execute format('grant all on kobo_data.%I to service_role', tbl);
  return tbl;
end;
$$;

-- Stores Kobo submissions [{kobo_id, uuid, submitted_at, data}]. Already
-- imported ones (same _id, even if edited or deleted here since) are never
-- duplicated nor overwritten. Returns {inserted, duplicates, last_kobo_id}.
create or replace function public.kobo_ingest(p_form_id uuid, p_rows jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm       kobo_forms;
  n_in      int;
  n_new     int := 0;
  n_total   int := 0;
  max_id    bigint;
begin
  select * into frm from kobo_forms where id = p_form_id for update;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  n_in := coalesce(jsonb_array_length(p_rows), 0);
  select max((r->>'kobo_id')::bigint) into max_id from jsonb_array_elements(coalesce(p_rows, '[]')) r;

  execute format(
    'with incoming as (
       select (r->>''kobo_id'')::bigint as kobo_id, r->>''uuid'' as kobo_uuid,
              (r->>''submitted_at'')::timestamptz as submitted_at, coalesce(r->''data'', ''{}'') as data
       from jsonb_array_elements($1) r
       where r->>''kobo_id'' is not null
         and (r->>''kobo_id'')::bigint > $4
     ), ins as (
       insert into kobo_data.%I (kobo_id, kobo_uuid, submitted_at, data, geom, source)
       select kobo_id, kobo_uuid, submitted_at, data,
              public.kobo_parse_geo(data->>$2, $3), ''kobo''
       from incoming
       on conflict (kobo_id) do nothing
       returning 1)
     select count(*) from ins', frm.table_name)
  into n_new
  using coalesce(p_rows, '[]'::jsonb), frm.geo_column, frm.geo_kind, frm.last_kobo_id;

  execute format('select count(*) from kobo_data.%I', frm.table_name) into n_total;
  update kobo_forms
     set last_kobo_id = greatest(last_kobo_id, coalesce(max_id, 0)),
         record_count = n_total,
         last_sync_at = now(), updated_at = now()
   where id = p_form_id;

  return jsonb_build_object('inserted', n_new, 'duplicates', n_in - n_new,
                            'last_kobo_id', greatest(frm.last_kobo_id, coalesce(max_id, 0)));
end;
$$;

-- After a manual change of the geographic column: recompute every geometry
-- from the stored answers (no new download from Kobo).
create or replace function public.kobo_recompute_geoms(p_form_id uuid)
returns int
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm kobo_forms;
  n   int;
begin
  select * into frm from kobo_forms where id = p_form_id;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  execute format('update kobo_data.%I set geom = public.kobo_parse_geo(data->>$1, $2)', frm.table_name)
  using frm.geo_column, frm.geo_kind;
  execute format('select count(*) from kobo_data.%I where geom is not null', frm.table_name) into n;
  return n;
end;
$$;

-- Records of a form, newest first, with an optional full-text filter.
create or replace function public.kobo_records(
  p_form_id uuid, p_limit int default 50, p_offset int default 0, p_search text default null)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm    kobo_forms;
  result jsonb;
begin
  select * into frm from kobo_forms where id = p_form_id;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  execute format(
    'with f as (
       select * from kobo_data.%I
       where $3 is null or data::text ilike ''%%'' || $3 || ''%%'')
     select jsonb_build_object(
       ''total'', (select count(*) from f),
       ''records'', coalesce((select jsonb_agg(jsonb_build_object(
           ''id'', id, ''kobo_id'', kobo_id, ''submitted_at'', submitted_at, ''source'', source,
           ''edited_at'', edited_at, ''data'', data, ''has_geom'', geom is not null)
           order by coalesce(submitted_at, created_at) desc)
         from (select * from f order by coalesce(submitted_at, created_at) desc
               limit $1 offset $2) x), ''[]''::jsonb))', frm.table_name)
  into result
  using greatest(1, least(coalesce(p_limit, 50), 500)), greatest(coalesce(p_offset, 0), 0),
        nullif(btrim(coalesce(p_search, '')), '');
  return result;
end;
$$;

create or replace function public.kobo_record(p_form_id uuid, p_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm    kobo_forms;
  result jsonb;
begin
  select * into frm from kobo_forms where id = p_form_id;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  execute format(
    'select jsonb_build_object(''id'', id, ''kobo_id'', kobo_id, ''submitted_at'', submitted_at,
            ''source'', source, ''edited_at'', edited_at, ''data'', data)
     from kobo_data.%I where id = $1', frm.table_name)
  into result using p_id;
  return result;
end;
$$;

-- Add (p_id null) or edit a record by hand. The geometry follows the
-- configured geographic column. Edited Kobo records are never overwritten
-- by a later synchronisation.
create or replace function public.kobo_record_save(p_form_id uuid, p_id uuid, p_data jsonb)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm    kobo_forms;
  new_id uuid;
begin
  select * into frm from kobo_forms where id = p_form_id;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  if p_data is null or jsonb_typeof(p_data) <> 'object' then
    raise exception 'Invalid record';
  end if;
  if p_id is null then
    execute format(
      'insert into kobo_data.%I (submitted_at, data, geom, source, edited_at)
       values (now(), $1, public.kobo_parse_geo($1->>$2, $3), ''manual'', now()) returning id',
      frm.table_name)
    into new_id using p_data, frm.geo_column, frm.geo_kind;
  else
    execute format(
      'update kobo_data.%I set data = $1, geom = public.kobo_parse_geo($1->>$2, $3), edited_at = now()
       where id = $4 returning id', frm.table_name)
    into new_id using p_data, frm.geo_column, frm.geo_kind, p_id;
    if new_id is null then
      raise exception 'Record not found';
    end if;
  end if;
  execute format('update kobo_forms set record_count = (select count(*) from kobo_data.%I),
                  updated_at = now() where id = $1', frm.table_name) using p_form_id;
  return new_id;
end;
$$;

create or replace function public.kobo_record_delete(p_form_id uuid, p_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm kobo_forms;
  n   int;
begin
  select * into frm from kobo_forms where id = p_form_id;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  execute format('delete from kobo_data.%I where id = $1', frm.table_name) using p_id;
  get diagnostics n = row_count;
  execute format('update kobo_forms set record_count = (select count(*) from kobo_data.%I),
                  updated_at = now() where id = $1', frm.table_name) using p_form_id;
  return n > 0;
end;
$$;

-- Map layer, light: {"v": version, "f": [[record id, GeoJSON geometry], ...]}
-- (6 decimals, ~0.1 m). Popups load one record on click (kobo_record).
create or replace function public.kobo_layer(p_form_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm    kobo_forms;
  result jsonb;
begin
  select * into frm from kobo_forms where id = p_form_id;
  if not found then
    raise exception 'Kobo form not found';
  end if;
  execute format(
    'select jsonb_build_object(
       ''v'', left(md5(coalesce(string_agg(id::text || coalesce(edited_at::text, ''''), '','' order by id), '''')
                     || coalesce($1, '''') || coalesce($2, '''')), 16),
       ''f'', coalesce(jsonb_agg(jsonb_build_array(id, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.000005), 6)::jsonb)), ''[]''::jsonb))
     from kobo_data.%I where geom is not null', frm.table_name)
  into result using frm.geo_column, frm.geo_kind;
  return result;
end;
$$;

create or replace function public.kobo_drop_form(p_form_id uuid)
returns void
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  tbl text;
begin
  select table_name into tbl from kobo_forms where id = p_form_id;
  if tbl is null then
    return;
  end if;
  if tbl !~ '^k_[a-f0-9]{32}$' then
    raise exception 'Kobo form not found';
  end if;
  execute format('drop table if exists kobo_data.%I', tbl);
  delete from kobo_forms where id = p_form_id;
end;
$$;

do $$
declare
  fn text;
begin
  foreach fn in array array[
    'kobo_parse_geo(text, text)', 'kobo_create_table(uuid)', 'kobo_ingest(uuid, jsonb)',
    'kobo_recompute_geoms(uuid)', 'kobo_records(uuid, int, int, text)', 'kobo_record(uuid, uuid)',
    'kobo_record_save(uuid, uuid, jsonb)', 'kobo_record_delete(uuid, uuid)',
    'kobo_layer(uuid)', 'kobo_drop_form(uuid)'] loop
    execute format('revoke all on function public.%s from public, anon, authenticated', fn);
    execute format('grant execute on function public.%s to service_role', fn);
  end loop;
end;
$$;

-- ============================================================================
-- Lot E - parcel sheets (fiches parcellaires)
-- ============================================================================
-- config: {"sections": [{"source": "parcel" | "audit" | "form:<formulaire id>",
--                        "title": text, "fields": [{"key": text, "label": text}]}]}
create table if not exists public.fiche_modeles (
  id          uuid primary key default gen_random_uuid(),
  owner       uuid not null references auth.users(id) on delete cascade,
  name        text not null check (length(btrim(name)) between 2 and 120),
  config      jsonb not null default '{"sections": []}'::jsonb
              check (jsonb_typeof(config -> 'sections') = 'array'),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists fiche_modeles_owner_idx on public.fiche_modeles (owner);

alter table public.fiche_modeles enable row level security;
drop policy if exists "Owners manage their sheet templates" on public.fiche_modeles;
create policy "Owners manage their sheet templates"
  on public.fiche_modeles for all
  using ((select auth.uid()) = owner)
  with check ((select auth.uid()) = owner);

-- Everything a sheet can show for each parcel of p_parcel_ids that p_uid
-- manages (cooperatives he owns): the parcel, its farmer and cooperative,
-- its latest bio audit, and its latest answer to each form of p_form_ids
-- (all versions; geometries as GeoJSON). Parcels outside his scope are
-- silently left out.
create or replace function public.fiche_parcel_data(
  p_uid uuid, p_parcel_ids uuid[], p_form_ids uuid[] default '{}')
returns jsonb
language plpgsql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  result  jsonb := '[]'::jsonb;
  par     record;
  item    jsonb;
  forms   jsonb;
  ver     record;
  ans     jsonb;
  best    jsonb;
  cols    text;
begin
  for par in
    select p.*, ST_AsGeoJSON(ST_SimplifyPreserveTopology(p.geom, 0.000005), 6)::jsonb as geojson,
           ST_Y(ST_PointOnSurface(p.geom)) as center_lat, ST_X(ST_PointOnSurface(p.geom)) as center_lng,
           f.full_name as farmer_name, f.id_card as farmer_id_card, f.gender as farmer_gender,
           f.year_of_birth as farmer_year_of_birth, c.name as cooperative_name
    from parcels p
    join cooperative c on c.uuid = p.cooperative_id
    left join farmers f on f.uuid = p.farmer_id
    where p.uuid = any(p_parcel_ids)
      and p.cooperative_id in (select x from public.fire_member_cooperatives(p_uid) x)
  loop
    item := jsonb_build_object(
      'uuid', par.uuid,
      'parcel', (to_jsonb(par) - 'geom' - 'geojson' - 'owner'),
      'geometry', par.geojson,
      'audit', (select to_jsonb(a) - 'owner' - 'auditor_signature'
                from audits_bio a where a.parcel_id = par.uuid
                order by a.audit_date desc, a.created_at desc limit 1));

    forms := '{}'::jsonb;
    for ver in
      select v.formulaire_id, v.version, v.table_name, v.fields
      from formulaire_versions v
      join formulaires fo on fo.id = v.formulaire_id
      where v.formulaire_id = any(coalesce(p_form_ids, '{}'))
        and fo.cooperative_id = par.cooperative_id
      order by v.formulaire_id, v.version desc
    loop
      select string_agg(
               case when fl->>'type' in ('geolocation', 'geoshape')
                    then format('ST_AsGeoJSON(r.%1$I, 7)::jsonb as %1$I', fl->>'key')
                    else format('r.%I', fl->>'key') end, ', ')
        into cols
      from jsonb_array_elements(ver.fields) fl;
      execute format(
        'select to_jsonb(x) from (select r.submitted_at%s from form_data.%I r
         where r.parcel_id = $1 order by r.submitted_at desc limit 1) x',
        coalesce(', ' || cols, ''), ver.table_name)
      into ans using par.uuid;
      if ans is not null then
        best := forms -> ver.formulaire_id::text;
        if best is null or (ans->>'submitted_at')::timestamptz > (best->>'submitted_at')::timestamptz then
          forms := forms || jsonb_build_object(ver.formulaire_id::text, ans);
        end if;
      end if;
    end loop;

    result := result || jsonb_build_array(item || jsonb_build_object('forms', forms));
  end loop;
  return result;
end;
$$;

revoke all on function public.fiche_parcel_data(uuid, uuid[], uuid[]) from public, anon, authenticated;
grant execute on function public.fiche_parcel_data(uuid, uuid[], uuid[]) to service_role;

notify pgrst, 'reload schema';
