-- ============================================================================
-- Smart Saha - offline synchronisation of the mobile app (agents, auditors)
-- Run once in the Supabase SQL editor, after 20261025_formulaires.sql.
--
-- Download (the app keeps a SQLite mirror of these rows):
--   * get_my_cooperative()           the member's cooperative
--   * get_my_cooperative_farmers()   every farmer of that cooperative
--   * get_my_cooperative_parcels()   every parcel, geom as GeoJSON text
-- The cooperative is ALWAYS read from the caller's own profile
-- (user_profiles.cooperative_id); only agents and auditors may call them.
-- user_profiles, formulaires, formulaire_versions, formulaire_enqueteurs and
-- audits_bio are read directly (their RLS policies already allow it).
--
-- Upload:
--   * insert_formulaire_response() can now also be called by a field agent
--     (role authenticated), for a published form assigned to them and only
--     under their own identity. Two optional reserved keys of p_values are
--     read: `id` (uuid generated on the phone: a retried upload doesn't
--     create a duplicate) and `submitted_at` (when the answer was collected
--     offline). Calls from the Flask server (service role) are unchanged.
--   * Storage policies of the bucket `formulaires`: agents upload the files
--     of their assigned forms (<form id>/...), auditors their audit
--     signatures (audits_bio/<their uid>/...).
-- Idempotent: can be run again without error.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Helper: cooperative of the calling agent / auditor, or an exception.
-- ----------------------------------------------------------------------------
create or replace function public.current_member_cooperative()
returns uuid
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  coop uuid;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;

  select p.cooperative_id into coop
  from user_profiles p
  join roles r on r.id = p.role_id
  where p.uuid = auth.uid() and r.role in ('agent', 'auditor');

  if not found then
    raise exception 'Only field agents and auditors can download this data';
  end if;
  if coop is null then
    raise exception 'Member is not attached to a cooperative';
  end if;

  return coop;
end;
$$;

revoke execute on function public.current_member_cooperative() from public, anon;
grant execute on function public.current_member_cooperative() to authenticated;

-- ----------------------------------------------------------------------------
-- Download RPCs
-- ----------------------------------------------------------------------------
create or replace function public.get_my_cooperative()
returns setof public.cooperative
language sql
stable
security definer
set search_path = public
as $$
  select c.* from cooperative c where c.uuid = public.current_member_cooperative();
$$;

create or replace function public.get_my_cooperative_farmers()
returns setof public.farmers
language sql
stable
security definer
set search_path = public
as $$
  select f.* from farmers f
  where f.cooperative_id = public.current_member_cooperative()
  order by f.full_name;
$$;

-- jsonb rows: every column of parcels, whatever was added since schema.sql,
-- with geom replaced by its GeoJSON text.
create or replace function public.get_my_cooperative_parcels()
returns setof jsonb
language sql
stable
security definer
set search_path = public, extensions
as $$
  select (to_jsonb(p) - 'geom') || jsonb_build_object('geom', ST_AsGeoJSON(p.geom))
  from parcels p
  where p.cooperative_id = public.current_member_cooperative()
  order by p.created_at desc;
$$;

revoke execute on function public.get_my_cooperative() from public, anon;
revoke execute on function public.get_my_cooperative_farmers() from public, anon;
revoke execute on function public.get_my_cooperative_parcels() from public, anon;
grant execute on function public.get_my_cooperative() to authenticated;
grant execute on function public.get_my_cooperative_farmers() to authenticated;
grant execute on function public.get_my_cooperative_parcels() to authenticated;

-- ----------------------------------------------------------------------------
-- RPC: insert_formulaire_response (replaces the 20261025 version)
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
  ver       public.formulaire_versions;
  coop      uuid;
  cols      text;
  new_id    uuid;
  client_id uuid := nullif(p_values->>'id', '')::uuid;
  v_at      timestamptz := coalesce(nullif(p_values->>'submitted_at', '')::timestamptz, now());
begin
  -- Mobile app (signed-in agent): only for themselves, on a published form
  -- they are assigned to. The service role (Flask) has no auth.uid().
  if auth.uid() is not null then
    if p_enqueteur_id is distinct from auth.uid() then
      raise exception 'You can only submit your own responses';
    end if;
    if not exists (
         select 1 from public.formulaire_enqueteurs e
         join public.formulaires f on f.id = e.formulaire_id
         where e.formulaire_id = p_formulaire_id and e.agent_id = auth.uid()
           and f.status = 'published') then
      raise exception 'This form is not assigned to you';
    end if;
  end if;

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
    'insert into form_data.%1$I (id, enqueteur_id, farmer_id, parcel_id, submitted_at, %2$s)
     select coalesce($5, gen_random_uuid()), $1, $2, $3, $6, %2$s
     from jsonb_populate_record(null::form_data.%1$I, $4)
     on conflict (id) do nothing
     returning id', ver.table_name, cols)
  into new_id
  using p_enqueteur_id, p_farmer_id, p_parcel_id, p_values, client_id, v_at;

  -- Already inserted by an earlier attempt of the same upload.
  return coalesce(new_id, client_id);
end;
$$;

revoke execute on function public.insert_formulaire_response(uuid, int, uuid, uuid, uuid, jsonb) from public, anon;
grant execute on function public.insert_formulaire_response(uuid, int, uuid, uuid, uuid, jsonb) to authenticated, service_role;

-- ----------------------------------------------------------------------------
-- Storage: uploads from the mobile app into the private bucket `formulaires`
-- ----------------------------------------------------------------------------
drop policy if exists "Agents upload files of assigned forms" on storage.objects;
create policy "Agents upload files of assigned forms"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'formulaires'
    and exists (
      select 1 from public.formulaire_enqueteurs e
      where e.agent_id = auth.uid()
        and e.formulaire_id::text = (storage.foldername(name))[1]));

drop policy if exists "Auditors upload their audit signatures" on storage.objects;
create policy "Auditors upload their audit signatures"
  on storage.objects for insert to authenticated
  with check (
    bucket_id = 'formulaires'
    and (storage.foldername(name))[1] = 'audits_bio'
    and (storage.foldername(name))[2] = auth.uid()::text);

notify pgrst, 'reload schema';
