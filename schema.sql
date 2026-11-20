-- ============================================================================
-- Smart Saha - schema for cooperative / farmers / parcels
-- Run this in the Supabase SQL editor (Database -> SQL Editor -> New query).
-- Assumes public.user_profiles already exists, as set up previously.
-- ============================================================================

create extension if not exists postgis;
create extension if not exists pgcrypto;   -- gen_random_uuid()

-- ----------------------------------------------------------------------------
-- cooperative
-- ----------------------------------------------------------------------------
create table if not exists public.cooperative (
  uuid              uuid primary key default gen_random_uuid(),
  name              text not null,
  president_name    text not null,
  email             text,
  mobile            text,
  address           text,
  legal_status      text not null default 'cooperative' check (legal_status in ('cooperative', 'group', 'other')),
  year_of_creation  int  not null check (year_of_creation between 1900 and 2100),
  status            text not null default 'active' check (status in ('active', 'not_active', 'na')),
  main_crop         text not null,
  second_crop       text,
  third_crop        text,
  crops             text[] not null default '{}',   -- every crop, in order (the first three are copied above)
  owner             uuid not null references auth.users(id) on delete cascade,
  created_at        timestamptz not null default now()
);

alter table public.cooperative enable row level security;

create policy "Owners manage their own cooperative"
  on public.cooperative for all
  using (auth.uid() = owner)
  with check (auth.uid() = owner);

-- ----------------------------------------------------------------------------
-- Farmers
-- ----------------------------------------------------------------------------
create table if not exists public.farmers (
  uuid              uuid primary key default gen_random_uuid(),
  cooperative_id    uuid not null references public.cooperative(uuid) on delete cascade,
  full_name         text not null,
  year_of_birth     int  not null check (year_of_birth between 1900 and 2025),
  gender            text not null check (gender in ('male', 'female', 'other')),
  id_card           text not null,
  handicap          boolean not null default false,
  marital_status    text not null check (marital_status in ('single', 'married', 'divorced', 'widowed', 'other')),
  household_number  int  not null check (household_number >= 0),
  status            text not null check (status in ('active', 'not_active', 'na')),
  owner             uuid not null references auth.users(id) on delete cascade,
  created_at        timestamptz not null default now()
);

create index if not exists farmers_cooperative_idx on public.farmers (cooperative_id);

alter table public.farmers enable row level security;

create policy "Owners manage their own farmers"
  on public.farmers for all
  using (auth.uid() = owner)
  with check (auth.uid() = owner);

-- ----------------------------------------------------------------------------
-- Parcels (PostGIS geometry, imported from KML / GeoPackage / Shapefile)
-- ----------------------------------------------------------------------------
create table if not exists public.parcels (
  uuid              uuid primary key default gen_random_uuid(),
  cooperative_id    uuid not null references public.cooperative(uuid) on delete cascade,
  farmer_id         uuid not null references public.farmers(uuid) on delete cascade,
  geom              geometry(Geometry, 4326) not null,
  area_ha           numeric,
  owner             uuid not null references auth.users(id) on delete cascade,
  created_at        timestamptz not null default now()
);

create index if not exists parcels_geom_gix on public.parcels using gist (geom);
create index if not exists parcels_cooperative_idx on public.parcels (cooperative_id);
create index if not exists parcels_farmer_idx on public.parcels (farmer_id);

alter table public.parcels enable row level security;

create policy "Owners manage their own parcels"
  on public.parcels for all
  using (auth.uid() = owner)
  with check (auth.uid() = owner);

-- ----------------------------------------------------------------------------
-- RPC: insert_parcel
-- The Flask app converts an uploaded KML/GPKG/Shapefile to GeoJSON with
-- geopandas, then calls this once per feature/shape found in the file.
-- SECURITY DEFINER so it can write the row, but it still checks the
-- cooperative/farmer belong to the caller and always sets owner = auth.uid()
-- itself - the caller can never pass someone else's uid.
-- ----------------------------------------------------------------------------
create or replace function public.insert_parcel(
  coop_id uuid,
  farmer_id_in uuid,
  geom_geojson jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public
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

  insert into parcels (cooperative_id, farmer_id, owner, geom, area_ha)
  values (coop_id, farmer_id_in, auth.uid(), g, round((ST_Area(g::geography) / 10000)::numeric, 4))
  returning uuid into new_id;

  return new_id;
end;
$$;

grant execute on function public.insert_parcel(uuid, uuid, jsonb) to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: get_my_parcels
-- Returns each of the current member's parcels with its geometry as GeoJSON
-- text, plus cooperative/farmer names, ready for the map + table.
-- ----------------------------------------------------------------------------
create or replace function public.get_my_parcels()
returns table (
  uuid              uuid,
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
set search_path = public
as $$
  select
    p.uuid, p.cooperative_id, c.name, p.farmer_id, f.full_name,
    p.area_ha, p.created_at, ST_AsGeoJSON(p.geom)
  from parcels p
  join cooperative c on c.uuid = p.cooperative_id
  join farmers f       on f.uuid = p.farmer_id
  where p.owner = auth.uid()
  order by p.created_at desc;
$$;

grant execute on function public.get_my_parcels() to authenticated;

-- ============================================================================
-- Smart Saha - Audit bio (organic export audit) per parcel
-- Run this in the Supabase SQL editor, after cooperative / farmers / parcels
-- already exist. Adjust "cooperative" below if your table is actually named
-- "cooperatives" in your project.
-- ============================================================================

create extension if not exists pgcrypto;   -- gen_random_uuid()

-- ----------------------------------------------------------------------------
-- audits_bio
-- One row per audit visit, always tied to a single parcel (parcel_id).
-- Sections follow the EU organic farming framework (Regulation (EU)
-- 2018/848): conversion status, inputs / practices, contamination
-- prevention, traceability registers, storage, non-conformities, and the
-- auditor's conclusion + signature.
-- ----------------------------------------------------------------------------
create table if not exists public.audits_bio (
  uuid                          uuid primary key default gen_random_uuid(),

  -- Identification: which parcel this audit covers
  parcel_id                     uuid not null references public.parcels(uuid) on delete cascade,
  auditor_name                  text not null,
  audit_date                    date not null default current_date,

  -- Historique de conversion
  conversion_start_date         date,
  conversion_status             text not null
                                 check (conversion_status in ('not_started', 'in_conversion', 'certified')),

  -- Pratiques agricoles
  inputs_used                   text,   -- fertilisers / plant protection products used
  seed_origin                   text,   -- origin of seeds / seedlings
  pest_management_method        text,

  -- Prévention des contaminations croisées
  neighboring_crops             text,
  buffer_zone_distance_m        numeric,
  contamination_risk            text not null default 'low'
                                 check (contamination_risk in ('low', 'medium', 'high')),

  -- Traçabilité (registres tenus)
  parcel_register_kept          boolean not null default false,
  inputs_register_kept          boolean not null default false,
  harvest_register_kept         boolean not null default false,
  sales_register_kept           boolean not null default false,
  bio_nonbio_separation         text,   -- how bio / non-bio flows are kept separate

  -- Stockage et manutention post-récolte
  storage_handling_notes        text,

  -- Constat de conformité
  non_conformities               text,
  non_conformity_severity        text not null default 'none'
                                 check (non_conformity_severity in ('none', 'minor', 'major', 'critical')),
  corrective_action_plan        text,
  corrective_action_deadline    date,

  -- Conclusion de l'audit
  overall_status                text not null
                                 check (overall_status in ('compliant', 'non_compliant', 'compliant_with_reservations')),
  auditor_signature             text,   -- base64 PNG data URL captured from the signature pad

  owner                          uuid not null references auth.users(id) on delete cascade,
  created_at                     timestamptz not null default now()
);

create index if not exists audits_bio_parcel_idx on public.audits_bio (parcel_id);
create index if not exists audits_bio_owner_idx on public.audits_bio (owner);

alter table public.audits_bio enable row level security;

create policy "Owners manage their own bio audits"
  on public.audits_bio for all
  using (auth.uid() = owner)
  with check (auth.uid() = owner);

-- ----------------------------------------------------------------------------
-- RPC: get_my_audits
-- Returns each of the current member's audits with the parcel/cooperative/
-- farmer names joined in, ready for the audit history table.
-- Adjust "cooperative" below to "cooperatives" if that's your table name.
-- ----------------------------------------------------------------------------
create or replace function public.get_my_audits()
returns table (
  uuid                      uuid,
  parcel_id                 uuid,
  cooperative_name          text,
  farmer_name                text,
  area_ha                    numeric,
  audit_date                 date,
  auditor_name                text,
  conversion_status           text,
  contamination_risk          text,
  non_conformity_severity     text,
  overall_status               text,
  created_at                   timestamptz
)
language sql
security definer
set search_path = public
as $$
  select
    a.uuid, a.parcel_id, c.name, f.full_name, p.area_ha,
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

