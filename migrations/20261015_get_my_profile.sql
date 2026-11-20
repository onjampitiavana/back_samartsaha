-- ============================================================================
-- Smart Saha - profile + role of the signed-in member (mobile app)
-- Run once in the Supabase SQL editor.
--
-- The Flutter app calls get_my_profile() right after sign-in to know the
-- member's role (public.roles.role) and redirect them: agent -> GPS parcel
-- tracing, other roles -> their dashboard. SECURITY DEFINER so the app
-- doesn't need read access to user_profiles / roles, but it only ever
-- returns the caller's own row.
-- Idempotent: can be run again without error.
-- ============================================================================

create or replace function public.get_my_profile()
returns table (
  uuid           uuid,
  username       text,
  full_name      text,
  email          text,
  role           text,
  cooperative_id uuid
)
language sql
stable
security definer
set search_path = public
as $$
  select p.uuid, p.username, p.full_name, p.email, r.role, p.cooperative_id
  from user_profiles p
  left join roles r on r.id = p.role_id
  where p.uuid = auth.uid();
$$;

revoke execute on function public.get_my_profile() from public, anon;
grant execute on function public.get_my_profile() to authenticated;

notify pgrst, 'reload schema';
