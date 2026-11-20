-- 1--
--CREATE EXTENSION postgis;

--ALTER TABLE farmers 
--ADD COLUMN cooperative_id UUID REFERENCES cooperative(uuid);

/*
ALTER TABLE farmers
ADD CONSTRAINT fk_farmer_coop
FOREIGN KEY (cooperative_id)
REFERENCES cooperative(uuid);
*/

--NOTIFY pgrst, 'reload schema';

/*
ALTER TABLE cooperative
ADD CONSTRAINT cooperative_uuid_unique UNIQUE (uuid);
*/

/*
create table if not exists public.parcels (
  uuid              uuid primary key default gen_random_uuid(),
  cooperative_id    uuid not null references public.cooperative(uuid) on delete cascade,
  farmer_id         uuid not null references public.farmers(uuid) on delete cascade,
  geom              geometry(Geometry, 4326) not null,
  area_ha           numeric,
  owner             uuid not null references auth.users(id) on delete cascade,
  created_at        timestamptz not null default now()
);
*/

/*
create index if not exists parcels_geom_gix on public.parcels using gist (geom);
create index if not exists parcels_cooperative_idx on public.parcels (cooperative_id);
create index if not exists parcels_farmer_idx on public.parcels (farmer_id);

*/

--2--

-- ----------------------------------------------------------------------------
-- RPC: insert_parcel
-- The Flask app converts an uploaded KML/GPKG/Shapefile to GeoJSON with
-- geopandas, then calls this once per feature/shape found in the file.
-- SECURITY DEFINER so it can write the row, but it still checks the
-- cooperative/farmer belong to the caller and always sets owner = auth.uid()
-- itself - the caller can never pass someone else's uid.
-- ----------------------------------------------------------------------------
/*
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
*/

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

  -- ::uuid côté colonne : sans effet si déjà uuid, corrige si stocké en text
  if not exists (
    select 1 from cooperative
    where uuid::uuid = coop_id and owner::uuid = auth.uid()
  ) then
    raise exception 'Cooperative not found';
  end if;

  if not exists (
    select 1 from farmers
    where uuid::uuid = farmer_id_in and owner::uuid = auth.uid()
  ) then
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

-- 3 --
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

--4 --

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

-- 5 --
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

--6 --
-- ----------------------------------------------------------------------------
-- Current status snapshot on each table (fast to filter/display)
-- ----------------------------------------------------------------------------
/*
alter table public.farmers
  add column if not exists bio_campaign_status text not null default 'considered'
  check (bio_campaign_status in ('considered', 'banned'));

alter table public.parcels
  add column if not exists bio_campaign_status text not null default 'considered'
  check (bio_campaign_status in ('considered', 'banned'));
*/


-- ----------------------------------------------------------------------------
-- bio_campaign_log
-- One row per status change. Exactly one of farmer_id / parcel_id is set,
-- enforced by the check constraint below.
-- ----------------------------------------------------------------------------
/*
create table if not exists public.bio_campaign_log (
  uuid              uuid primary key default gen_random_uuid(),
  entity_type       text not null check (entity_type in ('farmer', 'parcel')),
  farmer_id         uuid references public.farmers(uuid) on delete cascade,
  parcel_id         uuid references public.parcels(uuid) on delete cascade,
  status            text not null check (status in ('considered', 'banned')),
  reason            text,
  effective_date    date not null default current_date,
  owner             uuid not null references auth.users(id) on delete cascade,
  created_at        timestamptz not null default now(),
  constraint bio_campaign_log_entity_check check (
    (entity_type = 'farmer' and farmer_id is not null and parcel_id is null) or
    (entity_type = 'parcel' and parcel_id is not null and farmer_id is null)
  )
);

create index if not exists bio_campaign_log_farmer_idx on public.bio_campaign_log (farmer_id);
create index if not exists bio_campaign_log_parcel_idx on public.bio_campaign_log (parcel_id);
create index if not exists bio_campaign_log_owner_idx on public.bio_campaign_log (owner);
*/

-- ----------------------------------------------------------------------------
-- RPC: set_campaign_status
-- Updates the farmer/parcel's current status AND appends a log row, in a
-- single transaction so the two can never drift apart. SECURITY DEFINER so
-- it can write the log table (which has no INSERT policy for regular
-- clients), but it still checks the target row belongs to the caller.
-- ----------------------------------------------------------------------------
create or replace function public.set_campaign_status(
  entity_type_in text,
  entity_id_in uuid,
  new_status_in text,
  reason_in text default null
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  new_log_id uuid;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;
  if entity_type_in not in ('farmer', 'parcel') then
    raise exception 'Invalid entity type';
  end if;
  if new_status_in not in ('considered', 'banned') then
    raise exception 'Invalid status';
  end if;

  if entity_type_in = 'farmer' then
    if not exists (select 1 from farmers where uuid = entity_id_in and owner = auth.uid()) then
      raise exception 'Farmer not found';
    end if;
    update farmers set bio_campaign_status = new_status_in where uuid = entity_id_in;
    insert into bio_campaign_log (entity_type, farmer_id, status, reason, owner)
    values ('farmer', entity_id_in, new_status_in, reason_in, auth.uid())
    returning uuid into new_log_id;
  else
    if not exists (select 1 from parcels where uuid = entity_id_in and owner = auth.uid()) then
      raise exception 'Parcel not found';
    end if;
    update parcels set bio_campaign_status = new_status_in where uuid = entity_id_in;
    insert into bio_campaign_log (entity_type, parcel_id, status, reason, owner)
    values ('parcel', entity_id_in, new_status_in, reason_in, auth.uid())
    returning uuid into new_log_id;
  end if;

  return new_log_id;
end;
$$;

grant execute on function public.set_campaign_status(text, uuid, text, text) to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: get_campaign_log
-- Full history for the current member, with names joined in for display.
-- Adjust "cooperative" below to "cooperatives" if that's your table name.
-- ----------------------------------------------------------------------------
create or replace function public.get_campaign_log()
returns table (
  uuid              uuid,
  entity_type       text,
  entity_name       text,
  cooperative_name  text,
  status            text,
  reason            text,
  effective_date    date,
  created_at        timestamptz
)
language sql
security definer
set search_path = public
as $$
  select
    l.uuid,
    l.entity_type,
    coalesce(f.full_name, 'Parcel ' || substr(p.uuid::text, 1, 8)) as entity_name,
    coalesce(cf.name, cp.name) as cooperative_name,
    l.status, l.reason, l.effective_date, l.created_at
  from bio_campaign_log l
  left join farmers f      on f.uuid = l.farmer_id
  left join parcels p      on p.uuid = l.parcel_id
  left join cooperative cf on cf.uuid = f.cooperative_id
  left join cooperative cp on cp.uuid = p.cooperative_id
  where l.owner = auth.uid()
  order by l.created_at desc;
$$;

grant execute on function public.get_campaign_log() to authenticated;