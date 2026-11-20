-- ============================================================================
-- Smart Saha - admin / manager / cooperative hierarchy + query performance
-- Run once in the Supabase SQL editor (Database -> SQL Editor -> New query).
--
--   admin --(1..n)--> manager        user_profiles.admin_id (one admin per manager)
--   manager --(1..n)--> cooperative  cooperative.owner (the manager it belongs to)
--   admin --> staff (agent, auditor, consultant) in his managers' cooperatives
--             user_profiles.admin_id + user_profiles.cooperative_id
--
-- An admin creating a cooperative for one of his managers stores it with
-- owner = that manager (so the manager's Row Level Security keeps working
-- unchanged) and created_by = the admin. Cooperatives a manager creates
-- himself are visible to his admin through user_profiles.admin_id, and to
-- no other admin. The Flask app reads them with the service key.
--
-- Safe on existing data: only adds nullable columns and indexes, then links
-- existing staff to the admin owning their cooperative. Idempotent.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Hierarchy columns
-- ----------------------------------------------------------------------------
alter table public.user_profiles
  add column if not exists admin_id uuid
    references auth.users(id)
    on delete set null;

alter table public.cooperative
  add column if not exists created_by uuid
    references auth.users(id)
    on delete set null;

-- Existing staff attached to an admin's cooperative belong to that admin.
update public.user_profiles p
set admin_id = c.owner
from public.cooperative c
join public.user_profiles o on o.uuid = c.owner
join public.roles r         on r.id = o.role_id and r.role = 'admin'
where p.cooperative_id = c.uuid
  and p.admin_id is null
  and p.uuid <> c.owner;

update public.cooperative
set created_by = owner
where created_by is null;

-- ----------------------------------------------------------------------------
-- Indexes for the lookups done on every page
-- ----------------------------------------------------------------------------
create index if not exists user_profiles_admin_id_idx on public.user_profiles (admin_id);
create index if not exists user_profiles_role_id_idx  on public.user_profiles (role_id);
create index if not exists cooperative_owner_idx      on public.cooperative (owner);
create index if not exists farmers_owner_idx          on public.farmers (owner);
create index if not exists parcels_owner_idx          on public.parcels (owner);
create index if not exists parcels_owner_created_idx  on public.parcels (owner, created_at desc);
create index if not exists farmers_owner_created_idx  on public.farmers (owner, created_at desc);

-- ----------------------------------------------------------------------------
-- Row Level Security: evaluate auth.uid() once per query instead of once per
-- row (Supabase's recommended "(select auth.uid())" form). Same rules.
-- ----------------------------------------------------------------------------
do $$
begin
  if exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'cooperative'
             and policyname = 'Owners manage their own cooperative') then
    alter policy "Owners manage their own cooperative" on public.cooperative
      using ((select auth.uid()) = owner) with check ((select auth.uid()) = owner);
  end if;
  if exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'farmers'
             and policyname = 'Owners manage their own farmers') then
    alter policy "Owners manage their own farmers" on public.farmers
      using ((select auth.uid()) = owner) with check ((select auth.uid()) = owner);
  end if;
  if exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'parcels'
             and policyname = 'Owners manage their own parcels') then
    alter policy "Owners manage their own parcels" on public.parcels
      using ((select auth.uid()) = owner) with check ((select auth.uid()) = owner);
  end if;
end;
$$;

analyze public.user_profiles;
analyze public.cooperative;
analyze public.farmers;
analyze public.parcels;

-- Make PostgREST pick up the new columns immediately.
notify pgrst, 'reload schema';
