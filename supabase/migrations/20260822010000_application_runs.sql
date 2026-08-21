-- Phase 6.2: bounded-run identity for the Jobs selection -> worker flow.
--
-- Before this migration, "select N jobs and start the worker" had no
-- schema-level way to guarantee the worker only ever touches those N
-- jobs -- claim_next_queued_application() (20260820175616) claims the
-- oldest QUEUED row for a candidate, full stop, with no notion of "which
-- selection this belongs to". A local JSON-file run registry
-- (run_registry.py) stood in for this in the meantime, because the
-- Supabase CLI session available at development time wasn't authorized
-- for this project. Now that it is, this migration replaces that
-- workaround with the real mechanism: an application_runs table, a
-- run_id column on applications, and a claim function scoped to one
-- run_id instead of one candidate_id.
--
-- applications.run_id is nullable and unconstrained-in-practice for any
-- application enqueued outside the Jobs selection flow (the CLI's plain
-- --candidate-id path, resume_needs_input_application(), and everything
-- created before this migration) -- a run is an optional scope on top of
-- the existing candidate-claim model, not a replacement for it.

create table application_runs (
    id           uuid primary key default gen_random_uuid(),
    candidate_id uuid not null,
    status       text not null default 'QUEUED'
                     check (status in ('QUEUED', 'RUNNING', 'STOPPED', 'COMPLETE')),
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create trigger application_runs_set_updated_at
    before update on application_runs
    for each row execute function set_updated_at();

-- Same defense-in-depth pattern as every other table in the foundation
-- migration: RLS enabled with zero policies, plus an explicit table-level
-- revoke so a project-level default privilege can't hand anon/authenticated
-- access on its own.
alter table application_runs enable row level security;
revoke all on application_runs from anon, authenticated;

alter table applications
    add column run_id uuid references application_runs(id);

create index applications_run_idx on applications (run_id, status);

-- Same atomicity/eligibility semantics as claim_next_queued_application()
-- (FOR UPDATE SKIP LOCKED, blocked by an in-flight PROCESSING/SUBMITTING
-- application or an open SESSION_LEVEL intervention for the same
-- candidate) -- scoped to one run_id's rows instead of a whole
-- candidate's queue, so this function structurally cannot claim an
-- application outside the given run no matter what else is QUEUED.
create or replace function claim_next_queued_application_for_run(
    p_run_id uuid,
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
        select a1.id
        from applications a1
        where a1.run_id = p_run_id
          and a1.status = 'QUEUED'
          and not exists (
              select 1
              from applications a2
              where a2.candidate_id = a1.candidate_id
                and a2.status in ('PROCESSING', 'SUBMITTING')
          )
          and not exists (
              select 1
              from interventions iv
              join applications a3 on a3.id = iv.application_id
              where a3.candidate_id = a1.candidate_id
                and iv.status = 'OPEN'
                and iv.intervention_scope = 'SESSION_LEVEL'
          )
        order by a1.priority asc, a1.queued_at asc
        limit 1
        for update skip locked
    )
    returning *;
end;
$$;

revoke execute on function claim_next_queued_application_for_run(uuid, text) from public;
revoke execute on function claim_next_queued_application_for_run(uuid, text) from anon;
revoke execute on function claim_next_queued_application_for_run(uuid, text) from authenticated;
grant execute on function claim_next_queued_application_for_run(uuid, text) to service_role;
