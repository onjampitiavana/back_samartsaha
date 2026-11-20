
create extension if not exists postgis;

-- ----------------------------------------------------------------------------
-- 1. Checklist columns
-- ----------------------------------------------------------------------------
alter table public.audits_bio
  add column if not exists campaign                    text,
  add column if not exists crops                       text[]  not null default '{}',
  add column if not exists crop_cycle                  text,
  -- Pratiques agricoles
  add column if not exists seed_category               text,
  add column if not exists fertilization_methods       text,
  add column if not exists synthetic_fertilizers_used  boolean not null default false,
  add column if not exists synthetic_pesticides_used   boolean not null default false,
  add column if not exists gmo_used                    boolean not null default false,
  -- Prévention des contaminations
  add column if not exists neighbors_conventional      boolean not null default false,
  add column if not exists buffer_zone_present         boolean not null default false,
  add column if not exists hedges_present              boolean not null default false,
  add column if not exists buffer_zone_management      text,
  -- Traçabilité
  add column if not exists flows_separated             boolean not null default false,
  -- Stockage et manutention
  add column if not exists storage_dedicated           boolean not null default false,
  add column if not exists storage_clean               boolean not null default false,
  add column if not exists containers_labelled         boolean not null default false,
  -- Constat: [{description, severity, corrective_action, deadline, resolved, resolved_at}]
  add column if not exists non_conformity_items        jsonb   not null default '[]'::jsonb,
  -- Conclusion
  add column if not exists conclusion_notes            text,
  add column if not exists next_audit_due              date,
  add column if not exists area_ha_audited             numeric,
  add column if not exists updated_at                  timestamptz not null default now();

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'audits_bio_crop_cycle_check') then
    alter table public.audits_bio add constraint audits_bio_crop_cycle_check
      check (crop_cycle is null or crop_cycle in ('annual', 'perennial', 'grassland'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'audits_bio_seed_category_check') then
    alter table public.audits_bio add constraint audits_bio_seed_category_check
      check (seed_category is null
             or seed_category in ('organic', 'in_conversion', 'farm_saved', 'derogation', 'conventional'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'audits_bio_nc_items_check') then
    alter table public.audits_bio add constraint audits_bio_nc_items_check
      check (jsonb_typeof(non_conformity_items) = 'array');
  end if;
  if not exists (select 1 from pg_constraint where conname = 'audits_bio_campaign_check') then
    alter table public.audits_bio add constraint audits_bio_campaign_check
      check (campaign is null or char_length(campaign) between 4 and 20);
  end if;
end;
$$;

create index if not exists audits_bio_next_due_idx on public.audits_bio (next_audit_due);

-- ----------------------------------------------------------------------------
-- 2. Defaults computed by the database
-- ----------------------------------------------------------------------------
-- Area audited: geodesic area of the parcel outline at the time of the audit.
create or replace function public.audits_bio_before_insert()
returns trigger
language plpgsql
security definer                 -- reads the parcel outline whatever the caller's RLS
set search_path = public, extensions
as $$
begin
  new.campaign := coalesce(nullif(btrim(new.campaign), ''),
                           extract(year from new.audit_date)::int::text);
  -- Article 38(3): at least one verification of compliance per year.
  new.next_audit_due := coalesce(new.next_audit_due, (new.audit_date + interval '1 year')::date);
  select round((ST_Area(p.geom::geography) / 10000)::numeric, 4)
    into new.area_ha_audited
  from parcels p where p.uuid = new.parcel_id;
  new.non_conformity_items := coalesce(new.non_conformity_items, '[]'::jsonb);
  new.updated_at := now();
  return new;
end;
$$;

-- The non-conformities without their follow-up keys (resolved, resolved_at).
create or replace function public.audit_bio_nc_core(items jsonb)
returns jsonb
language sql
immutable
as $$
  select coalesce(jsonb_agg(e - 'resolved' - 'resolved_at' order by n), '[]'::jsonb)
  from jsonb_array_elements(coalesce(items, '[]'::jsonb)) with ordinality t(e, n);
$$;

-- 3. A recorded audit is final: only the follow-up of its non-conformities
--    (resolved / resolved_at of each item) may change.
create or replace function public.audits_bio_before_update()
returns trigger
language plpgsql
as $$
begin
  if (to_jsonb(new) - 'non_conformity_items' - 'updated_at')
       is distinct from (to_jsonb(old) - 'non_conformity_items' - 'updated_at')
     or public.audit_bio_nc_core(new.non_conformity_items)
       is distinct from public.audit_bio_nc_core(old.non_conformity_items) then
    raise exception 'Validated audit is locked';
  end if;
  new.updated_at := now();
  return new;
end;
$$;

-- Backfill before (re)creating the lock (the backfill is an update).
drop trigger if exists audits_bio_lock on public.audits_bio;
drop trigger if exists audits_bio_defaults on public.audits_bio;

update public.audits_bio a set
  campaign       = coalesce(a.campaign, extract(year from a.audit_date)::int::text),
  next_audit_due = coalesce(a.next_audit_due, (a.audit_date + interval '1 year')::date),
  area_ha_audited = coalesce(a.area_ha_audited, (
    select round((ST_Area(p.geom::geography) / 10000)::numeric, 4)
    from public.parcels p where p.uuid = a.parcel_id)),
  -- The single free-text finding of older audits becomes one item.
  non_conformity_items = case
    when jsonb_array_length(a.non_conformity_items) = 0
         and nullif(btrim(a.non_conformities), '') is not null
    then jsonb_build_array(jsonb_build_object(
           'description', btrim(a.non_conformities),
           'severity', case when a.non_conformity_severity in ('minor', 'major', 'critical')
                            then a.non_conformity_severity else 'minor' end,
           'corrective_action', a.corrective_action_plan,
           'deadline', a.corrective_action_deadline,
           'resolved', false))
    else a.non_conformity_items end
where a.campaign is null or a.next_audit_due is null or a.area_ha_audited is null;

create trigger audits_bio_defaults
  before insert on public.audits_bio
  for each row execute function public.audits_bio_before_insert();

create trigger audits_bio_lock
  before update on public.audits_bio
  for each row execute function public.audits_bio_before_update();

-- ----------------------------------------------------------------------------
-- 4. Drafts (work in progress of the web form)
-- ----------------------------------------------------------------------------
create table if not exists public.audits_bio_drafts (
  uuid        uuid primary key default gen_random_uuid(),
  parcel_id   uuid not null references public.parcels(uuid) on delete cascade,
  owner       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  data        jsonb not null default '{}'::jsonb
                check (jsonb_typeof(data) = 'object' and pg_column_size(data) < 800000),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists audits_bio_drafts_owner_idx  on public.audits_bio_drafts (owner, updated_at desc);
create index if not exists audits_bio_drafts_parcel_idx on public.audits_bio_drafts (parcel_id);

-- ----------------------------------------------------------------------------
-- 5. Row Level Security
-- ----------------------------------------------------------------------------
-- May the caller audit this parcel? His own parcel (manager: parcels.owner),
-- or a parcel of his cooperative if he is an auditor (mobile app).
create or replace function public.audit_bio_can_audit(p_parcel uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from parcels p
    where p.uuid = p_parcel
      and (p.owner = auth.uid()
           or p.cooperative_id = (
             select up.cooperative_id
             from user_profiles up
             join roles r on r.id = up.role_id
             where up.uuid = auth.uid() and r.role = 'auditor')));
$$;

revoke execute on function public.audit_bio_can_audit(uuid) from public, anon;
grant execute on function public.audit_bio_can_audit(uuid) to authenticated;

alter table public.audits_bio        enable row level security;
alter table public.audits_bio_drafts enable row level security;

-- audits_bio keeps "Owners manage their own bio audits" (schema.sql): the
-- auditor reads and inserts his own audits from the mobile app.
drop policy if exists "Managers read the audits of their parcels" on public.audits_bio;
create policy "Managers read the audits of their parcels"
  on public.audits_bio for select to authenticated
  using (exists (select 1 from public.parcels p
                 where p.uuid = audits_bio.parcel_id and p.owner = (select auth.uid())));

drop policy if exists "Managers follow up the audits of their parcels" on public.audits_bio;
create policy "Managers follow up the audits of their parcels"
  on public.audits_bio for update to authenticated
  using (exists (select 1 from public.parcels p
                 where p.uuid = audits_bio.parcel_id and p.owner = (select auth.uid())))
  with check (exists (select 1 from public.parcels p
                      where p.uuid = audits_bio.parcel_id and p.owner = (select auth.uid())));

drop policy if exists "Audits only for parcels in scope" on public.audits_bio;
create policy "Audits only for parcels in scope"
  on public.audits_bio as restrictive for insert to authenticated
  with check (owner = (select auth.uid()) and public.audit_bio_can_audit(parcel_id));

drop policy if exists "Recorded audits are never deleted" on public.audits_bio;
create policy "Recorded audits are never deleted"
  on public.audits_bio as restrictive for delete to authenticated
  using (false);

drop policy if exists "Owners manage their audit drafts" on public.audits_bio_drafts;
create policy "Owners manage their audit drafts"
  on public.audits_bio_drafts for all to authenticated
  using (owner = (select auth.uid()))
  with check (owner = (select auth.uid()) and public.audit_bio_can_audit(parcel_id));

-- ----------------------------------------------------------------------------
-- 6. Page RPCs (security invoker: the caller's RLS decides what he sees)
-- ----------------------------------------------------------------------------
-- Audit Bio page: the manager's parcels, every audit of them (no signature,
-- with the count of open non-conformities) and his drafts.
create or replace function public.get_audits_bio_page()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  with my_parcels as (
    select p.uuid, p.nom, p.cooperative_id, p.farmer_id, p.area_ha
    from parcels p
    where p.owner = (select auth.uid())
  )
  select jsonb_build_object(
    'parcels', coalesce((
      select jsonb_agg(jsonb_build_object(
               'uuid', p.uuid, 'nom', p.nom, 'area_ha', p.area_ha,
               'cooperative_id', p.cooperative_id, 'cooperative_name', c.name,
               'farmer_id', p.farmer_id, 'farmer_name', f.full_name)
             order by c.name, f.full_name, p.nom)
      from my_parcels p
      left join cooperative c on c.uuid = p.cooperative_id
      left join farmers f     on f.uuid = p.farmer_id), '[]'::jsonb),
    'audits', coalesce((
      select jsonb_agg(jsonb_build_object(
               'uuid', a.uuid, 'parcel_id', a.parcel_id, 'campaign', a.campaign,
               'audit_date', a.audit_date, 'auditor_name', a.auditor_name,
               'owner', a.owner, 'conversion_status', a.conversion_status,
               'overall_status', a.overall_status,
               'non_conformity_severity', a.non_conformity_severity,
               'next_audit_due', a.next_audit_due, 'area_ha_audited', a.area_ha_audited,
               'created_at', a.created_at,
               'nc_total', jsonb_array_length(a.non_conformity_items),
               'nc_open', (select count(*) from jsonb_array_elements(a.non_conformity_items) e
                           where not coalesce(e -> 'resolved' = 'true'::jsonb, false)),
               'nc_next_deadline', (select min((e ->> 'deadline')::date)
                                    from jsonb_array_elements(a.non_conformity_items) e
                                    where not coalesce(e -> 'resolved' = 'true'::jsonb, false)
                                      and (e ->> 'deadline') ~ '^\d{4}-\d{2}-\d{2}$'))
             order by a.audit_date desc, a.created_at desc)
      from audits_bio a
      where a.parcel_id in (select uuid from my_parcels)), '[]'::jsonb),
    'drafts', coalesce((
      select jsonb_agg(jsonb_build_object(
               'uuid', d.uuid, 'parcel_id', d.parcel_id,
               'audit_date', d.data ->> 'audit_date', 'campaign', d.data ->> 'campaign',
               'updated_at', d.updated_at)
             order by d.updated_at desc)
      from audits_bio_drafts d
      where d.owner = (select auth.uid())), '[]'::jsonb)
  );
$$;

-- Pre-filled identification of a new audit: parcel (area recomputed by
-- PostGIS), farmer, cooperative and its crops, and the latest audit of the
-- parcel (conversion history). NULL if the parcel is not the caller's.
create or replace function public.audit_bio_parcel_context(p_parcel uuid)
returns jsonb
language sql
stable
security invoker
set search_path = public, extensions
as $$
  select jsonb_build_object(
    'parcel', jsonb_build_object(
      'uuid', p.uuid,
      'nom', p.nom,
      'area_ha', p.area_ha,
      'area_ha_postgis', round((ST_Area(p.geom::geography) / 10000)::numeric, 4),
      'perimeter_m', round(ST_Perimeter(p.geom::geography)::numeric, 1),
      'lat', round(ST_Y(ST_PointOnSurface(p.geom))::numeric, 6),
      'lng', round(ST_X(ST_PointOnSurface(p.geom))::numeric, 6),
      'bio_campaign_status', p.bio_campaign_status,
      'created_at', p.created_at),
    'cooperative', jsonb_build_object(
      'uuid', c.uuid,
      'name', c.name,
      'crops', case when cardinality(c.crops) > 0 then to_jsonb(c.crops)
                    else to_jsonb(array_remove(array[c.main_crop, c.second_crop, c.third_crop], null)) end),
    'farmer', jsonb_build_object('uuid', f.uuid, 'full_name', f.full_name, 'id_card', f.id_card),
    'last_audit', (
      select to_jsonb(a) - 'auditor_signature'
      from audits_bio a
      where a.parcel_id = p.uuid
      order by a.audit_date desc, a.created_at desc
      limit 1),
    'audit_count', (select count(*) from audits_bio a where a.parcel_id = p.uuid)
  )
  from parcels p
  left join cooperative c on c.uuid = p.cooperative_id
  left join farmers f     on f.uuid = p.farmer_id
  where p.uuid = p_parcel and p.owner = (select auth.uid());
$$;

revoke execute on function public.get_audits_bio_page() from public, anon;
revoke execute on function public.audit_bio_parcel_context(uuid) from public, anon;
grant execute on function public.get_audits_bio_page() to authenticated;
grant execute on function public.audit_bio_parcel_context(uuid) to authenticated;

analyze public.audits_bio;

notify pgrst, 'reload schema';
