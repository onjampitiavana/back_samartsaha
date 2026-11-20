-- ============================================================================
-- Smart Saha - staff accounts: active / inactive (Gestion du personnel).
-- Run once in the Supabase SQL editor, AFTER 20261029_admin_cooperatives.sql.
-- Idempotent.
--
--   user_profiles.is_active : false = account deactivated by its
--   administrator. Flask also bans the Supabase Auth user (no sign-in, no
--   session refresh) and lifts the ban on reactivation.
--   admin_personnel_overview : same as before, plus is_active.
-- ============================================================================

alter table public.user_profiles
  add column if not exists is_active boolean not null default true;

create or replace function public.admin_personnel_overview(p_admin uuid)
returns jsonb
language sql
stable
set search_path = public
as $$
  with managers as (
    select p.uuid, p.username, p.full_name, p.email, p.mobile, p.role_id,
           p.cooperative_id, p.admin_id, p.created_at, p.is_active
    from user_profiles p
    join roles r on r.id = p.role_id and r.role = 'manager'
    where p.admin_id = p_admin
  ),
  coops as (
    select c.uuid, c.name, c.owner
    from cooperative c
    where c.owner = p_admin
       or c.owner in (select uuid from managers)
  ),
  members as (
    select p.uuid, p.username, p.full_name, p.email, p.mobile, p.role_id,
           p.cooperative_id, p.admin_id, p.created_at, p.is_active
    from user_profiles p
    where p.admin_id = p_admin
       or p.cooperative_id in (select uuid from coops)
  )
  select jsonb_build_object(
    'managers',     coalesce((select jsonb_agg(to_jsonb(m) order by m.created_at desc) from managers m), '[]'::jsonb),
    'cooperatives', coalesce((select jsonb_agg(to_jsonb(c) order by c.name)            from coops c),    '[]'::jsonb),
    'members',      coalesce((select jsonb_agg(to_jsonb(x) order by x.created_at desc) from members x),  '[]'::jsonb)
  );
$$;

revoke all on function public.admin_personnel_overview(uuid) from public, anon, authenticated;
grant execute on function public.admin_personnel_overview(uuid) to service_role;

notify pgrst, 'reload schema';
