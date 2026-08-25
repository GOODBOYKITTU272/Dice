-- Phase M8B: candidate-scoped Dice auth state, replacing the single
-- global DICE_AUTH_COOKIES_JSON env var (one Dice account for every
-- candidate) with a real per-candidate secret record.
--
-- Storage choice: Supabase Vault (pgsodium-backed, already available on
-- this hosted project -- no new Python dependency, no home-rolled
-- encryption key to manage). The raw cookie JSON is never stored in a
-- plain table; only a pointer (vault_secret_id) lives here, and the
-- actual decrypt only ever happens inside a SECURITY DEFINER function
-- granted to service_role alone -- anon/authenticated can reach neither
-- this table nor the vault schema at all (vault isn't even exposed
-- through PostgREST -- confirmed live: "Invalid schema: vault").
--
-- Deliberately NOT a duplicate of the frontend's browser_profiles table
-- (applywizz-oneclick, project zusouzxgomeonsswqygw) -- that table
-- already exists for a future self-service "Connect Dice" flow, but
-- this project has no dependency on that schema and shouldn't reach
-- into it. This table is scoped to Dice's own operational domain, same
-- as dice_auth_health right next to it.
create extension if not exists supabase_vault cascade;

create table dice_candidate_auth_state (
    id               uuid primary key default gen_random_uuid(),
    candidate_id     uuid not null unique,
    vault_secret_id  uuid not null,
    status           text not null default 'ACTIVE' check (status in ('ACTIVE', 'INVALIDATED')),
    provisioned_by   text not null default 'operator_manual',
    invalidated_at   timestamptz,
    invalidated_reason text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create trigger dice_candidate_auth_state_set_updated_at
    before update on dice_candidate_auth_state
    for each row execute function set_updated_at();

alter table dice_candidate_auth_state enable row level security;
revoke all on dice_candidate_auth_state from anon, authenticated;

-- Creates or replaces (on reconnect) the one auth-state record for a
-- candidate. Never a second row per candidate -- the unique constraint
-- on candidate_id makes "one candidate, one current Dice session" a
-- database-level guarantee, not just an application convention.
create or replace function dice_auth_state_set(
    p_candidate_id uuid,
    p_cookies_json text,
    p_provisioned_by text default 'operator_manual'
)
returns uuid
language plpgsql
security definer
set search_path = public, vault, pg_temp
as $$
declare
    v_existing dice_candidate_auth_state%rowtype;
    v_secret_id uuid;
begin
    select * into v_existing from dice_candidate_auth_state where candidate_id = p_candidate_id;

    if v_existing.id is not null then
        perform vault.update_secret(v_existing.vault_secret_id, p_cookies_json);
        update dice_candidate_auth_state
        set status = 'ACTIVE',
            provisioned_by = p_provisioned_by,
            invalidated_at = null,
            invalidated_reason = null
        where candidate_id = p_candidate_id;
        return v_existing.id;
    end if;

    v_secret_id := vault.create_secret(p_cookies_json, 'dice_auth:' || p_candidate_id::text);
    insert into dice_candidate_auth_state (candidate_id, vault_secret_id, provisioned_by)
    values (p_candidate_id, v_secret_id, p_provisioned_by);
    return v_secret_id;
end;
$$;

-- Returns the decrypted cookie JSON for exactly this candidate, or null
-- if none exists or it's been invalidated -- callers must treat null as
-- AUTH_REQUIRED, never fall back to any other candidate's state.
create or replace function dice_auth_state_get(p_candidate_id uuid)
returns text
language plpgsql
security definer
set search_path = public, vault, pg_temp
as $$
declare
    v_state dice_candidate_auth_state%rowtype;
    v_secret text;
begin
    select * into v_state from dice_candidate_auth_state
    where candidate_id = p_candidate_id and status = 'ACTIVE';

    if v_state.id is null then
        return null;
    end if;

    select decrypted_secret into v_secret from vault.decrypted_secrets where id = v_state.vault_secret_id;
    return v_secret;
end;
$$;

create or replace function dice_auth_state_invalidate(p_candidate_id uuid, p_reason text)
returns void
language sql
security definer
set search_path = public, pg_temp
as $$
    update dice_candidate_auth_state
    set status = 'INVALIDATED', invalidated_at = now(), invalidated_reason = p_reason
    where candidate_id = p_candidate_id;
$$;

revoke all on function dice_auth_state_set(uuid, text, text) from public, anon, authenticated;
revoke all on function dice_auth_state_get(uuid) from public, anon, authenticated;
revoke all on function dice_auth_state_invalidate(uuid, text) from public, anon, authenticated;
grant execute on function dice_auth_state_set(uuid, text, text) to service_role;
grant execute on function dice_auth_state_get(uuid) to service_role;
grant execute on function dice_auth_state_invalidate(uuid, text) to service_role;
