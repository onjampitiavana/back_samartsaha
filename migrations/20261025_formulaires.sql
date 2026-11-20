-- ============================================================================
-- Smart Saha - survey forms ("formulaires") built by administrators
-- Run once in the Supabase SQL editor, after schema.sql and the 20261015_*
-- migrations.
--
--   public.formulaires              one row per form: metadata, cooperative,
--                                   and the working copy of its fields (jsonb)
--   public.formulaire_versions      one row per PUBLISHED version: the frozen
--                                   field list and the name of its table
--   public.formulaire_enqueteurs    which field agents may answer which form
--                                   (many-to-many)
--   form_data.f_<form id>_v<n>      one table per published version, created
--                                   by publish_formulaire(): one column per
--                                   field + enqueteur_id, farmer_id,
--                                   parcel_id, submitted_at
--
-- Versioning: publishing never alters an existing response table. Each
-- publication freezes the current fields into a new version with its own
-- table, so responses already collected keep the exact structure they were
-- entered with.
--
-- DDL safety: the response tables are built only inside publish_formulaire(),
-- from a whitelist of field types. Column keys are checked against
-- ^[a-z][a-z0-9_]{0,47}$, every identifier goes through format('%I') and
-- every literal through format('%L'). The RPCs below are callable by the
-- service role only (the Flask server checks the member's role first).
--
-- Also creates the private Storage bucket `formulaires` (photos, signatures).
-- Idempotent: can be run again without error.
-- ============================================================================

create extension if not exists pgcrypto;

create schema if not exists form_data;
revoke all on schema form_data from public, anon, authenticated;
grant usage on schema form_data to service_role;

-- ----------------------------------------------------------------------------
-- Tables
-- ----------------------------------------------------------------------------
create table if not exists public.formulaires (
  id                 uuid primary key default gen_random_uuid(),
  title              text not null check (length(trim(title)) between 2 and 160),
  description        text,
  cooperative_id     uuid not null references public.cooperative(uuid) on delete restrict,
  status             text not null default 'draft'
                     check (status in ('draft', 'published', 'archived')),
  fields             jsonb not null default '[]'::jsonb
                     check (jsonb_typeof(fields) = 'array'),
  current_version    int,                   -- latest published version, null if never
  has_draft_changes  boolean not null default false,
  created_by         uuid not null references auth.users(id) on delete restrict,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists formulaires_cooperative_idx on public.formulaires (cooperative_id);

create table if not exists public.formulaire_versions (
  id             uuid primary key default gen_random_uuid(),
  formulaire_id  uuid not null references public.formulaires(id) on delete restrict,
  version        int  not null check (version >= 1),
  fields         jsonb not null,
  table_name     text not null unique,
  published_by   uuid not null references auth.users(id) on delete restrict,
  published_at   timestamptz not null default now(),
  unique (formulaire_id, version)
);

create table if not exists public.formulaire_enqueteurs (
  formulaire_id  uuid not null references public.formulaires(id) on delete cascade,
  agent_id       uuid not null references auth.users(id) on delete cascade,
  assigned_by    uuid references auth.users(id) on delete set null,
  assigned_at    timestamptz not null default now(),
  primary key (formulaire_id, agent_id)
);

create index if not exists formulaire_enqueteurs_agent_idx
  on public.formulaire_enqueteurs (agent_id);

-- Written by the Flask server with the service role. Agents (mobile app)
-- may read the forms assigned to them.
alter table public.formulaires           enable row level security;
alter table public.formulaire_versions   enable row level security;
alter table public.formulaire_enqueteurs enable row level security;

drop policy if exists "Agents read their assignments" on public.formulaire_enqueteurs;
create policy "Agents read their assignments"
  on public.formulaire_enqueteurs for select
  using (agent_id = auth.uid());

drop policy if exists "Agents read assigned forms" on public.formulaires;
create policy "Agents read assigned forms"
  on public.formulaires for select
  using (status = 'published' and exists (
    select 1 from public.formulaire_enqueteurs e
    where e.formulaire_id = formulaires.id and e.agent_id = auth.uid()));

drop policy if exists "Agents read versions of assigned forms" on public.formulaire_versions;
create policy "Agents read versions of assigned forms"
  on public.formulaire_versions for select
  using (exists (
    select 1 from public.formulaire_enqueteurs e
    where e.formulaire_id = formulaire_versions.formulaire_id
      and e.agent_id = auth.uid()));

-- ----------------------------------------------------------------------------
-- Helper: SQL column definition of one field (raises on anything unexpected)
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- RPC: publish_formulaire
-- Freezes the form's current fields into version n+1 and creates its
-- response table, in one transaction. Returns the new version number.
-- ----------------------------------------------------------------------------
create or replace function public.publish_formulaire(p_formulaire_id uuid, p_actor uuid)
returns int
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  frm      public.formulaires;
  v        int;
  tbl      text;
  cols     text;
  n_fields int;
  n_keys   int;
begin
  select * into frm from public.formulaires where id = p_formulaire_id for update;
  if not found then
    raise exception 'Form not found';
  end if;
  if frm.status = 'archived' then
    raise exception 'Archived forms cannot be published';
  end if;

  select count(*), count(distinct f->>'key') into n_fields, n_keys
  from jsonb_array_elements(frm.fields) f;
  if n_fields = 0 then
    raise exception 'A form needs at least one field';
  end if;
  if n_fields > 150 then
    raise exception 'A form can have at most 150 fields';
  end if;
  if n_keys <> n_fields then
    raise exception 'Field keys must be unique';
  end if;

  if frm.current_version is not null and exists (
       select 1 from public.formulaire_versions
       where formulaire_id = frm.id and version = frm.current_version
         and fields = frm.fields) then
    raise exception 'Nothing to publish: the fields did not change';
  end if;

  v   := coalesce(frm.current_version, 0) + 1;
  tbl := format('f_%s_v%s', replace(frm.id::text, '-', ''), v);

  select string_agg(public.formulaire_column_sql(f), ', ' order by ord)
    into cols
  from jsonb_array_elements(frm.fields) with ordinality as x(f, ord);

  execute format(
    'create table form_data.%I (
       id           uuid primary key default gen_random_uuid(),
       enqueteur_id uuid not null references auth.users(id) on delete restrict,
       farmer_id    uuid references public.farmers(uuid) on delete set null,
       parcel_id    uuid references public.parcels(uuid) on delete set null,
       submitted_at timestamptz not null default now(),
       %s)', tbl, cols);
  execute format('create index on form_data.%I (enqueteur_id)', tbl);
  execute format('create index on form_data.%I (submitted_at desc)', tbl);
  execute format('alter table form_data.%I enable row level security', tbl);
  execute format('revoke all on form_data.%I from public, anon, authenticated', tbl);
  execute format('grant all on form_data.%I to service_role', tbl);

  insert into public.formulaire_versions (formulaire_id, version, fields, table_name, published_by)
  values (frm.id, v, frm.fields, tbl, p_actor);

  update public.formulaires
     set current_version = v, status = 'published',
         has_draft_changes = false, updated_at = now()
   where id = frm.id;

  return v;
end;
$$;

revoke execute on function public.publish_formulaire(uuid, uuid) from public, anon, authenticated;
grant execute on function public.publish_formulaire(uuid, uuid) to service_role;

-- ----------------------------------------------------------------------------
-- RPC: insert_formulaire_response
-- p_values: {field key: value}; geolocation as EWKT 'SRID=4326;POINT(lng lat)',
-- multiple choice as a JSON array. Unknown keys are ignored.
-- ----------------------------------------------------------------------------
create or replace function public.insert_formulaire_response(
  p_formulaire_id uuid,
  p_version       int,
  p_enqueteur_id  uuid,
  p_farmer_id     uuid,
  p_parcel_id     uuid,
  p_values        jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  ver    public.formulaire_versions;
  coop   uuid;
  cols   text;
  new_id uuid;
begin
  select v.* into ver from public.formulaire_versions v
  where v.formulaire_id = p_formulaire_id and v.version = p_version;
  if not found then
    raise exception 'Form version not found';
  end if;
  select cooperative_id into coop from public.formulaires where id = p_formulaire_id;

  if p_farmer_id is not null and not exists (
       select 1 from public.farmers where uuid = p_farmer_id and cooperative_id = coop) then
    raise exception 'Farmer not found in this cooperative';
  end if;
  if p_parcel_id is not null and not exists (
       select 1 from public.parcels where uuid = p_parcel_id and cooperative_id = coop
         and (p_farmer_id is null or farmer_id = p_farmer_id)) then
    raise exception 'Parcel not found for this farmer';
  end if;

  select string_agg(format('%I', f->>'key'), ', ') into cols
  from jsonb_array_elements(ver.fields) f;

  execute format(
    'insert into form_data.%1$I (enqueteur_id, farmer_id, parcel_id, %2$s)
     select $1, $2, $3, %2$s from jsonb_populate_record(null::form_data.%1$I, $4)
     returning id', ver.table_name, cols)
  into new_id
  using p_enqueteur_id, p_farmer_id, p_parcel_id, p_values;

  return new_id;
end;
$$;

revoke execute on function public.insert_formulaire_response(uuid, int, uuid, uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.insert_formulaire_response(uuid, int, uuid, uuid, uuid, jsonb) to service_role;

-- ----------------------------------------------------------------------------
-- RPC: get_formulaire_responses
-- Responses of one version as a JSON array (newest first), with the names of
-- the agent and farmer. Geolocation comes back as GeoJSON.
-- p_enqueteur_id restricts to one agent's responses (null: all).
-- ----------------------------------------------------------------------------
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
           case when f->>'type' = 'geolocation'
                then format('ST_AsGeoJSON(r.%1$I)::jsonb as %1$I', f->>'key')
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

-- ----------------------------------------------------------------------------
-- RPC: get_formulaire_stats
-- Tracking dashboard: totals per version and per agent, over all versions.
-- ----------------------------------------------------------------------------
create or replace function public.get_formulaire_stats(p_formulaire_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  ver   record;
  parts text[] := '{}';
  union_sql text;
  result jsonb;
begin
  for ver in select version, table_name from public.formulaire_versions
             where formulaire_id = p_formulaire_id order by version loop
    parts := parts || format('select %s as version, enqueteur_id, submitted_at from form_data.%I',
                             ver.version, ver.table_name);
  end loop;
  if array_length(parts, 1) is null then
    return jsonb_build_object('total', 0, 'versions', '[]'::jsonb, 'enqueteurs', '[]'::jsonb);
  end if;
  union_sql := array_to_string(parts, ' union all ');

  execute format(
    'with r as (%s)
     select jsonb_build_object(
       ''total'', (select count(*) from r),
       ''last_at'', (select max(submitted_at) from r),
       ''versions'', coalesce((select jsonb_agg(jsonb_build_object(''version'', version, ''total'', n) order by version)
                               from (select version, count(*) n from r group by version) s), ''[]''::jsonb),
       ''enqueteurs'', coalesce((select jsonb_agg(jsonb_build_object(
                                   ''enqueteur_id'', s.enqueteur_id, ''name'', p.full_name,
                                   ''total'', s.n, ''last_at'', s.last_at) order by s.n desc)
                                 from (select enqueteur_id, count(*) n, max(submitted_at) last_at
                                       from r group by enqueteur_id) s
                                 left join public.user_profiles p on p.uuid = s.enqueteur_id), ''[]''::jsonb))',
    union_sql)
  into result;

  return result;
end;
$$;

revoke execute on function public.get_formulaire_stats(uuid) from public, anon, authenticated;
grant execute on function public.get_formulaire_stats(uuid) to service_role;

-- ----------------------------------------------------------------------------
-- Storage bucket for photos and signatures (private: served by signed URLs)
-- ----------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('formulaires', 'formulaires', false)
on conflict (id) do nothing;

notify pgrst, 'reload schema';
