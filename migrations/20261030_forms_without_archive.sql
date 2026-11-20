-- ============================================================================
-- Smart Saha - survey forms no longer have an "archived" status.
-- Run once in the Supabase SQL editor (after 20261025_formulaires.sql).
-- Safe on existing data, idempotent.
--
-- Archived forms go back to what they were before being archived: published
-- if they have a published version (their responses are kept), draft
-- otherwise. Then the status can only be 'draft' or 'published'.
-- publish_formulaire() still contains a check for 'archived': it simply never
-- matches any more, so the function is left as it is.
-- ============================================================================

update public.formulaires
set status = case when current_version is null then 'draft' else 'published' end,
    updated_at = now()
where status = 'archived';

do $$
declare
  c record;
begin
  -- the check constraint on status, whatever its generated name
  for c in
    select con.conname
    from pg_constraint con
    join pg_attribute att on att.attrelid = con.conrelid and att.attnum = any (con.conkey)
    where con.conrelid = 'public.formulaires'::regclass
      and con.contype = 'c'
      and att.attname = 'status'
  loop
    execute format('alter table public.formulaires drop constraint %I', c.conname);
  end loop;
end;
$$;

alter table public.formulaires
  add constraint formulaires_status_check check (status in ('draft', 'published'));

notify pgrst, 'reload schema';
