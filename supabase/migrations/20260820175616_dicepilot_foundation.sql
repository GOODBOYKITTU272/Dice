-- DicePilot Phase 1 foundation schema.
-- Project: pkuqcnvtweukgurisczw (dedicated, empty, separate from the Indeed
-- Supabase project). Does not touch any Indeed table.
--
-- Design choices explained in the Phase 1 report delivered alongside this
-- migration; not applied to the live project until that review is approved.

create extension if not exists pgcrypto;

-- Shared trigger: keep updated_at accurate even if application code forgets
-- to set it explicitly.
create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ── dice_jobs ────────────────────────────────────────────────────────────
-- Canonical Dice job discovery record, independent of any candidate.
create table dice_jobs (
    id                  uuid primary key default gen_random_uuid(),
    dice_job_id         text not null unique,
    canonical_url       text not null,
    title               text not null,
    company_name        text,
    location            text,
    employment_type     text,
    is_third_party      boolean,
    description         text,
    c2c_status          text not null default 'UNKNOWN'
                            check (c2c_status in ('CONFIRMED', 'LIKELY', 'NOT_C2C', 'UNKNOWN')),
    c2c_reason          text,
    is_easy_apply       boolean not null default false,
    easy_apply_evidence jsonb,
    discovered_at       timestamptz not null default now(),
    last_checked_at     timestamptz,
    raw_metadata        jsonb,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create trigger dice_jobs_set_updated_at
    before update on dice_jobs
    for each row execute function set_updated_at();

-- ── applications ─────────────────────────────────────────────────────────
-- Candidate-specific application queue and durable execution state.
create table applications (
    id                   uuid primary key default gen_random_uuid(),
    candidate_id         uuid not null,
    dice_job_id          uuid not null references dice_jobs(id) on delete restrict,
    status               text not null default 'QUEUED'
                             check (status in (
                                 'QUEUED', 'PROCESSING', 'NEEDS_INPUT', 'SUBMITTING',
                                 'SUBMITTED', 'FAILED_RETRYABLE', 'FAILED'
                             )),
    current_step         text,
    priority             integer not null default 100,
    attempt_count        integer not null default 0,
    worker_id            text,
    lock_acquired_at     timestamptz,
    resume_source_url    text,
    error_code           text,
    error_message        text,
    verification_evidence jsonb,
    queued_at            timestamptz not null default now(),
    started_at           timestamptz,
    submitted_at         timestamptz,
    finished_at          timestamptz,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),

    constraint applications_candidate_job_unique unique (candidate_id, dice_job_id)
);

create index applications_claim_idx
    on applications (candidate_id, status, priority, queued_at);

create trigger applications_set_updated_at
    before update on applications
    for each row execute function set_updated_at();

-- ── application_events ───────────────────────────────────────────────────
-- Append-only execution timeline. No update/delete path is exposed by the
-- repository layer; enforced by convention, not by a DB trigger, since the
-- service-role key always has full DML access regardless.
create table application_events (
    id             uuid primary key default gen_random_uuid(),
    application_id uuid not null references applications(id) on delete cascade,
    event_type     text not null,
    step           text,
    message        text,
    metadata       jsonb,
    created_at     timestamptz not null default now()
);

create index application_events_timeline_idx
    on application_events (application_id, created_at);

-- ── interventions ────────────────────────────────────────────────────────
-- Unknown question / security action requiring human input.
--
-- intervention_scope (V1 Decision 2) is what actually drives queue-claim
-- behavior, independent of `type`:
--   APPLICATION_LEVEL — blocks only this application; worker may claim a
--     different QUEUED application for the same candidate.
--   SESSION_LEVEL     — blocks the whole candidate/browser worker; no new
--     claim until this is resolved (shared Dice browser profile is unsafe
--     to keep using).
create table interventions (
    id                 uuid primary key default gen_random_uuid(),
    application_id     uuid not null references applications(id) on delete cascade,
    type               text not null
                           check (type in (
                               'UNKNOWN_QUESTION', 'AUTHENTICATION', 'SECURITY_ACTION',
                               'MISSING_CANDIDATE_FACT', 'OTHER'
                           )),
    intervention_scope text not null
                           check (intervention_scope in ('APPLICATION_LEVEL', 'SESSION_LEVEL')),
    question_text      text,
    options            jsonb,
    status             text not null default 'OPEN'
                           check (status in ('OPEN', 'ANSWERED', 'CANCELLED')),
    answer             jsonb,
    answered_by        text,
    created_at         timestamptz not null default now(),
    resolved_at        timestamptz
);

create index interventions_open_idx
    on interventions (application_id, status);

-- Supports the claim function's SESSION_LEVEL block-check.
create index interventions_scope_status_idx
    on interventions (intervention_scope, status);

-- ── browser_profiles ─────────────────────────────────────────────────────
-- Operational metadata only — no cookies/session material stored here.
create table browser_profiles (
    id                     uuid primary key default gen_random_uuid(),
    candidate_id           uuid not null,
    platform               text not null default 'dice',
    profile_key            text not null,
    status                 text not null default 'NEEDS_LOGIN'
                               check (status in ('ACTIVE', 'NEEDS_LOGIN', 'NEEDS_ACTION', 'DISABLED')),
    last_authenticated_at  timestamptz,
    last_used_at           timestamptz,
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),

    constraint browser_profiles_candidate_platform_unique unique (candidate_id, platform)
);

create trigger browser_profiles_set_updated_at
    before update on browser_profiles
    for each row execute function set_updated_at();

-- ── Row Level Security ───────────────────────────────────────────────────
-- Enabled with zero policies: anon/authenticated get no row access by
-- default; service_role bypasses RLS as usual and remains the only way in.
alter table dice_jobs enable row level security;
alter table applications enable row level security;
alter table application_events enable row level security;
alter table interventions enable row level security;
alter table browser_profiles enable row level security;

-- Defense in depth: also revoke table-level grants from anon/authenticated
-- explicitly, rather than relying on RLS alone. A fresh Supabase project's
-- default privileges (ALTER DEFAULT PRIVILEGES ... GRANT ALL ON TABLES)
-- would otherwise hand these roles full SQL-level grants on any new table,
-- with RLS as the only thing standing between them and the data. This way,
-- even a misconfigured or dropped RLS policy later isn't the sole barrier.
revoke all on dice_jobs, applications, application_events, interventions, browser_profiles
    from anon, authenticated;

-- ── Atomic queue claim ───────────────────────────────────────────────────
-- Single-statement UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)
-- so claiming is atomic without an in-memory Python lock. Per V1 Decision 4:
--   - blocks if the candidate already has a PROCESSING or SUBMITTING
--     application (sequential execution guarantee)
--   - blocks if the candidate has an unresolved (OPEN) SESSION_LEVEL
--     intervention on any of their applications (shared browser profile
--     is unsafe to keep using until that's resolved)
--   - does NOT block on APPLICATION_LEVEL NEEDS_INPUT — that application
--     just sits there; an independent QUEUED job can still be claimed
create or replace function claim_next_queued_application(
    p_candidate_id uuid,
    p_worker_id text
)
returns setof applications
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update applications
    set status = 'PROCESSING',
        worker_id = p_worker_id,
        lock_acquired_at = now(),
        started_at = coalesce(started_at, now()),
        updated_at = now()
    where id = (
        select id
        from applications
        where candidate_id = p_candidate_id
          and status = 'QUEUED'
          and not exists (
              select 1
              from applications a2
              where a2.candidate_id = p_candidate_id
                and a2.status in ('PROCESSING', 'SUBMITTING')
          )
          and not exists (
              select 1
              from interventions iv
              join applications a3 on a3.id = iv.application_id
              where a3.candidate_id = p_candidate_id
                and iv.status = 'OPEN'
                and iv.intervention_scope = 'SESSION_LEVEL'
          )
        order by priority asc, queued_at asc
        limit 1
        for update skip locked
    )
    returning *;
end;
$$;

-- SECURITY DEFINER means this function runs with its owner's (the
-- migration-running role's) privileges, not the caller's — necessary so it
-- can update applications regardless of the caller's own row-level access.
-- search_path is pinned to `public` to prevent search_path-hijacking
-- against a SECURITY DEFINER function (the classic footgun for this
-- pattern). EXECUTE defaults to PUBLIC on function creation in Postgres,
-- which combined with SECURITY DEFINER would let *any* role reachable via
-- PostgREST (including anon, via the RPC endpoint) invoke a privileged,
-- RLS-bypassing update. Lock that down explicitly: only service_role
-- (i.e. the backend worker) may call it.
revoke execute on function claim_next_queued_application(uuid, text) from public;
grant execute on function claim_next_queued_application(uuid, text) to service_role;
