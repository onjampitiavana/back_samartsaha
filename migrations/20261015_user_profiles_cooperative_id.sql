-- ============================================================================
-- Smart Saha - link member profiles to a cooperative
-- Run once in the Supabase SQL editor.
--
-- Adds a nullable user_profiles.cooperative_id -> cooperative.uuid.
-- Used by the staff management page (/admin/personnel): staff accounts
-- (manager, auditor, consultant) created by an admin get the cooperative the
-- admin owns (cooperative.owner = admin uid).
--
-- Safe to run on existing data: no row is changed or deleted, existing
-- profiles simply get NULL. If a cooperative is deleted, its staff profiles
-- are kept and their cooperative_id goes back to NULL.
-- Idempotent: can be run again without error.
-- ============================================================================

alter table public.user_profiles
  add column if not exists cooperative_id uuid
    references public.cooperative(uuid)
    on delete set null;

create index if not exists user_profiles_cooperative_id_idx
  on public.user_profiles (cooperative_id);

-- Make PostgREST pick up the new column immediately.
notify pgrst, 'reload schema';
