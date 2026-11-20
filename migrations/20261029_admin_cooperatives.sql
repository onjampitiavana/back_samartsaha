-- ============================================================================
-- Smart Saha - administrator: staff page in one query, dynamic crops,
-- defaults for the cooperative columns the form no longer asks for.
-- Run once in the Supabase SQL editor, AFTER 20261028_role_hierarchy.sql
-- (it uses user_profiles.admin_id). Safe on existing data, idempotent.
--
-- Flask keeps working before this file is run: it falls back to three
-- queries for the staff page and writes at most three crops.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Crops: any number per cooperative.
--    cooperative.crops holds the full list, in order. main_crop / second_crop
--    / third_crop are kept and still written (the first three), so older
--    code, exports and the mobile app keep reading them.
-- ----------------------------------------------------------------------------
alter table public.cooperative
  add column if not exists crops text[] not null default '{}';

update public.cooperative
set crops = array_remove(array[
      nullif(main_crop, ''), nullif(second_crop, ''), nullif(third_crop, '')
    ], null)
where crops = '{}';

-- ----------------------------------------------------------------------------
-- 2. Legal status and status: no longer in the form, kept in the table.
--    Same columns, same checks; they only gain a default.
-- ----------------------------------------------------------------------------
alter table public.cooperative alter column legal_status set default 'cooperative';
alter table public.cooperative alter column status       set default 'active';

-- ----------------------------------------------------------------------------
-- 3. The administrator's staff page in one round trip
--    (core/services/personnel_service.py: _load_admin_rows).
--    managers     : profiles with role 'manager' whose admin_id = p_admin
--    cooperatives : owned by p_admin or by one of those managers
--    members      : attached to those cooperatives, or whose admin_id = p_admin
--    Columns match PROFILE_LIST_COLUMNS in core/models/repositories.py.
-- ----------------------------------------------------------------------------
create or replace function public.admin_personnel_overview(p_admin uuid)
returns jsonb
language sql
stable
set search_path = public
as $$
  with managers as (
    select p.uuid, p.username, p.full_name, p.email, p.mobile, p.role_id,
           p.cooperative_id, p.admin_id, p.created_at
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
           p.cooperative_id, p.admin_id, p.created_at
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

-- Only the server (service key) may call it: it reads every profile.
revoke all on function public.admin_personnel_overview(uuid) from public, anon, authenticated;
grant execute on function public.admin_personnel_overview(uuid) to service_role;

-- Lookups used by the function (the first two exist since 20261028).
create index if not exists user_profiles_admin_id_idx       on public.user_profiles (admin_id);
create index if not exists cooperative_owner_idx            on public.cooperative (owner);
create index if not exists user_profiles_cooperative_id_idx on public.user_profiles (cooperative_id);

analyze public.cooperative;
analyze public.user_profiles;

-- Make PostgREST see the new column and function immediately.
notify pgrst, 'reload schema';
