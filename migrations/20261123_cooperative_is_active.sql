-- ============================================================================
-- Smart Saha - cooperative active / inactive (boolean), and what it blocks.
-- Run once in the Supabase SQL editor, AFTER every earlier migration
-- (it references formulaires, fire_alerts, kobo_forms, audits_bio).
-- Safe on existing data, idempotent.
--
-- 1. cooperative.is_active boolean NOT NULL DEFAULT true.
--    The historical text column `status` ('active' / 'not_active' / 'na') is
--    kept for the mobile app and the exports, and kept in sync by a trigger:
--      is_active = true  <->  status = 'active'
--      is_active = false <->  status = 'not_active'
-- 2. An inactive cooperative accepts no new farmer, parcel, audit bio or
--    staff member (triggers: the web app, the mobile app and any direct
--    PostgREST call are all refused).
-- 3. A cooperative can only be deleted when it is inactive AND nothing is
--    attached to it any more (farmers, parcels, audits, staff, forms, fire
--    alerts, Kobo forms). Without this, ON DELETE CASCADE would silently
--    wipe its farmers and parcels.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. The column, filled from the existing status
-- ----------------------------------------------------------------------------
alter table public.cooperative
  add column if not exists is_active boolean not null default true;

update public.cooperative
set is_active = (status is distinct from 'not_active')
where is_active is distinct from (status is distinct from 'not_active');

update public.cooperative
set status = case when is_active then 'active' else 'not_active' end
where status is distinct from case when is_active then 'active' else 'not_active' end;

create index if not exists cooperative_is_active_idx on public.cooperative (is_active);

create or replace function public.cooperative_sync_status()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'INSERT' then
    -- An older client may still send only status = 'not_active'.
    if new.status = 'not_active' then
      new.is_active := false;
    end if;
  elsif new.is_active is distinct from old.is_active then
    null;                                   -- is_active changed: it wins
  elsif new.status is distinct from old.status then
    new.is_active := (new.status is distinct from 'not_active');
  end if;
  new.status := case when new.is_active then 'active' else 'not_active' end;
  return new;
end;
$$;

drop trigger if exists cooperative_sync_status on public.cooperative;
create trigger cooperative_sync_status
  before insert or update of is_active, status on public.cooperative
  for each row execute function public.cooperative_sync_status();

-- ----------------------------------------------------------------------------
-- 2. Nothing new attached to an inactive cooperative
-- ----------------------------------------------------------------------------
create or replace function public.cooperative_assert_active(p_cooperative uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_cooperative is not null and exists (
       select 1 from cooperative where uuid = p_cooperative and not is_active) then
    raise exception 'cooperative is inactive' using errcode = 'P0001';
  end if;
end;
$$;

-- farmers, parcels: cooperative_id on the row. Only when the row is added
-- to the cooperative (insert, or moved to it): editing an existing farmer of
-- an inactive cooperative stays possible.
create or replace function public.cooperative_guard_row()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if tg_op = 'INSERT' or new.cooperative_id is distinct from old.cooperative_id then
    perform cooperative_assert_active(new.cooperative_id);
  end if;
  return new;
end;
$$;

drop trigger if exists farmers_cooperative_active on public.farmers;
create trigger farmers_cooperative_active
  before insert or update of cooperative_id on public.farmers
  for each row execute function public.cooperative_guard_row();

drop trigger if exists parcels_cooperative_active on public.parcels;
create trigger parcels_cooperative_active
  before insert or update of cooperative_id on public.parcels
  for each row execute function public.cooperative_guard_row();

-- user_profiles (staff): cooperative_id may be NULL (admins, managers).
drop trigger if exists user_profiles_cooperative_active on public.user_profiles;
create trigger user_profiles_cooperative_active
  before insert or update of cooperative_id on public.user_profiles
  for each row execute function public.cooperative_guard_row();

-- audits_bio: the cooperative is the parcel's.
create or replace function public.audits_bio_cooperative_active()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  perform cooperative_assert_active(
    (select cooperative_id from parcels where uuid = new.parcel_id));
  return new;
end;
$$;

drop trigger if exists audits_bio_cooperative_active on public.audits_bio;
create trigger audits_bio_cooperative_active
  before insert on public.audits_bio
  for each row execute function public.audits_bio_cooperative_active();

-- ----------------------------------------------------------------------------
-- 3. Deletion: inactive and nothing attached
-- ----------------------------------------------------------------------------
-- What is still attached, as {"farmers": n, "parcels": n, ...} (zeros left
-- out). Used by the trigger below and by Flask for the user's message.
create or replace function public.cooperative_linked_counts(p_cooperative uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  counts jsonb;
begin
  select jsonb_strip_nulls(jsonb_build_object(
    'farmers',     nullif((select count(*) from farmers       where cooperative_id = p_cooperative), 0),
    'parcels',     nullif((select count(*) from parcels       where cooperative_id = p_cooperative), 0),
    'audits',      nullif((select count(*) from audits_bio a join parcels p on p.uuid = a.parcel_id
                           where p.cooperative_id = p_cooperative), 0),
    'staff',       nullif((select count(*) from user_profiles where cooperative_id = p_cooperative), 0),
    'forms',       nullif((select count(*) from formulaires   where cooperative_id = p_cooperative), 0),
    'fire_alerts', nullif((select count(*) from fire_alerts   where cooperative_id = p_cooperative), 0),
    'kobo_forms',  nullif((select count(*) from kobo_forms    where cooperative_id = p_cooperative), 0)
  )) into counts;
  return counts;
end;
$$;

grant execute on function public.cooperative_linked_counts(uuid) to authenticated;

create or replace function public.cooperative_guard_delete()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if old.is_active then
    raise exception 'cooperative must be deactivated before deletion' using errcode = 'P0001';
  end if;
  if cooperative_linked_counts(old.uuid) <> '{}'::jsonb then
    raise exception 'cooperative still has linked data' using errcode = 'P0001';
  end if;
  return old;
end;
$$;

drop trigger if exists cooperative_guard_delete on public.cooperative;
create trigger cooperative_guard_delete
  before delete on public.cooperative
  for each row execute function public.cooperative_guard_delete();
