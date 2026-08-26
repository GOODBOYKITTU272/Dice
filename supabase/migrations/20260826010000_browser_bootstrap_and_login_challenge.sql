-- Phase F2B (revised): a Dice-owned browser login trust chain --
-- operator-issued bootstrap code -> Telegram-approved login challenge ->
-- the existing HMAC customer session token (db/customer_identity.py).
--
-- Deliberately NOT built on attention_link_codes/candidate_attention_
-- channels: that table's channel CHECK constraint only permits
-- ('TELEGRAM', 'IMESSAGE'), and consume_link_code() has notification-
-- channel-binding side effects (attention/channels.py::bind_channel) that
-- do not apply here -- a bootstrap code is not a messaging channel. Two
-- new, fully separate tables instead.
--
-- No refresh/renewal table yet (deliberate V1 scope cut) -- this slice
-- proves bootstrap code -> Telegram approval -> the existing 5-minute
-- access token, end to end, before any session-renewal mechanism is
-- added.

create table browser_bootstrap_codes (
    code_hash    text primary key,
    candidate_id uuid not null,
    expires_at   timestamptz not null,
    consumed_at  timestamptz,
    created_at   timestamptz not null default now()
);

alter table browser_bootstrap_codes enable row level security;
revoke all on browser_bootstrap_codes from anon, authenticated;

create table browser_login_challenges (
    id                    uuid primary key default gen_random_uuid(),
    candidate_id          uuid not null,
    challenge_secret_hash text not null,
    status                text not null default 'PENDING'
                            check (status in ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED', 'CONSUMED')),
    created_at            timestamptz not null default now(),
    expires_at            timestamptz not null,
    approved_at           timestamptz,
    denied_at             timestamptz,
    consumed_at           timestamptz
);

alter table browser_login_challenges enable row level security;
revoke all on browser_login_challenges from anon, authenticated;

create index browser_login_challenges_candidate_idx on browser_login_challenges (candidate_id);

-- Atomic claims, same pattern/rationale as claim_next_queued_application()
-- (20260820175616_dicepilot_foundation.sql): a single UPDATE ... WHERE
-- <still-eligible> RETURNING * is what makes each of these safe under
-- concurrent callers across multiple Railway processes -- Postgres row
-- locking guarantees only one concurrent UPDATE against the same row can
-- ever see its own WHERE clause still match. No RPC here does its
-- eligibility check as a separate SELECT-then-UPDATE.

create or replace function consume_browser_bootstrap_code(p_code_hash text)
returns setof browser_bootstrap_codes
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update browser_bootstrap_codes
    set consumed_at = now()
    where code_hash = p_code_hash
      and consumed_at is null
      and expires_at > now()
    returning *;
end;
$$;

revoke execute on function consume_browser_bootstrap_code(text) from public;
revoke execute on function consume_browser_bootstrap_code(text) from anon;
revoke execute on function consume_browser_bootstrap_code(text) from authenticated;
grant execute on function consume_browser_bootstrap_code(text) to service_role;

create or replace function approve_browser_login_challenge(p_challenge_id uuid)
returns setof browser_login_challenges
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update browser_login_challenges
    set status = 'APPROVED', approved_at = now()
    where id = p_challenge_id
      and status = 'PENDING'
      and expires_at > now()
    returning *;
end;
$$;

revoke execute on function approve_browser_login_challenge(uuid) from public;
revoke execute on function approve_browser_login_challenge(uuid) from anon;
revoke execute on function approve_browser_login_challenge(uuid) from authenticated;
grant execute on function approve_browser_login_challenge(uuid) to service_role;

create or replace function deny_browser_login_challenge(p_challenge_id uuid)
returns setof browser_login_challenges
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update browser_login_challenges
    set status = 'DENIED', denied_at = now()
    where id = p_challenge_id
      and status = 'PENDING'
    returning *;
end;
$$;

revoke execute on function deny_browser_login_challenge(uuid) from public;
revoke execute on function deny_browser_login_challenge(uuid) from anon;
revoke execute on function deny_browser_login_challenge(uuid) from authenticated;
grant execute on function deny_browser_login_challenge(uuid) to service_role;

-- The session-exchange atomicity guarantee (Phase 9/14B): only one of two
-- concurrent exchange attempts against the same APPROVED challenge can
-- ever flip it to CONSUMED, so only one access token is ever issued.
create or replace function consume_browser_login_challenge(p_challenge_id uuid)
returns setof browser_login_challenges
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update browser_login_challenges
    set status = 'CONSUMED', consumed_at = now()
    where id = p_challenge_id
      and status = 'APPROVED'
      and expires_at > now()
    returning *;
end;
$$;

revoke execute on function consume_browser_login_challenge(uuid) from public;
revoke execute on function consume_browser_login_challenge(uuid) from anon;
revoke execute on function consume_browser_login_challenge(uuid) from authenticated;
grant execute on function consume_browser_login_challenge(uuid) to service_role;

-- Telegram-send-failure recovery (Phase 13): if the approval message
-- can't actually be delivered, the challenge must not sit as PENDING
-- forever with no way for the candidate to ever approve it. Expiring it
-- immediately server-side (never re-marking a consumed bootstrap code as
-- unconsumed) is what forces a clean re-issue rather than an ambiguous
-- half-alive state.
create or replace function expire_browser_login_challenge(p_challenge_id uuid)
returns setof browser_login_challenges
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update browser_login_challenges
    set status = 'EXPIRED'
    where id = p_challenge_id
      and status = 'PENDING'
    returning *;
end;
$$;

revoke execute on function expire_browser_login_challenge(uuid) from public;
revoke execute on function expire_browser_login_challenge(uuid) from anon;
revoke execute on function expire_browser_login_challenge(uuid) from authenticated;
grant execute on function expire_browser_login_challenge(uuid) to service_role;
