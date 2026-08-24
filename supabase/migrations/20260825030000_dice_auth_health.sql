-- Phase 8 (Readiness Gate, PART A): durable Dice auth-health signal, one
-- row per candidate. Real gap this closes, live-found 2026-08-25:
-- nothing durable ever recorded "we know Dice auth is currently good/
-- bad" -- every check was a fresh live browser hit, and there was no
-- way to cheaply avoid offering a job the system already knows will
-- fail. is_healthy is the single source of truth the readiness gate
-- reads; last_verified_at is when it was last POSITIVELY confirmed
-- (never just "assumed still good because nothing failed yet");
-- invalidated_at/invalidated_reason record the most recent known
-- AUTH_REQUIRED, which must immediately flip is_healthy to false --
-- a stale cached "healthy" is exactly the failure mode this table
-- exists to prevent.
create table dice_auth_health (
    candidate_id       uuid primary key,
    is_healthy         boolean not null default false,
    last_verified_at   timestamptz,
    last_checked_at    timestamptz,
    invalidated_at      timestamptz,
    invalidated_reason text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create trigger dice_auth_health_set_updated_at
    before update on dice_auth_health
    for each row execute function set_updated_at();

alter table dice_auth_health enable row level security;
revoke all on dice_auth_health from anon, authenticated;
