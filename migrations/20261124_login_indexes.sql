-- ============================================================================
-- Smart Saha - indexes for the sign-in lookups. Optional, idempotent.
--
-- Every sign-in reads the member's profile and role by e-mail (in parallel
-- with the password check, core/services/auth_service.py) and every
-- authenticated request may read it by uuid (lookup_role). The table
-- definition is not in this repository: these indexes are created only if
-- they don't exist yet, whatever the existing constraints.
-- ============================================================================

create index if not exists user_profiles_uuid_idx  on public.user_profiles (uuid);
create index if not exists user_profiles_email_idx on public.user_profiles (email);
