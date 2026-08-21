-- Phase 6.3: separates the Vercel frontend from the actual worker.
--
-- Before this migration, "Apply to Selected Jobs" (whichever process
-- served the request -- local Flask or the Vercel deployment) launched
-- the worker itself via subprocess.Popen. That only ever worked when
-- Flask was running locally, on the same machine as the dedicated,
-- authenticated Dice Chrome -- Vercel's servers have no path to
-- 127.0.0.1:9333 and no ability to keep a detached child process alive
-- past the request, so a run started from the deployed site just sat
-- QUEUED forever with nothing ever processing it.
--
-- The fix is a real poll/claim split: Vercel (or local Flask -- same
-- code either way) only ever writes a run as PENDING; a separate,
-- standalone daemon (dice_browser/worker_daemon.py), started manually on
-- the Mac that has the authenticated Chrome, polls for PENDING runs,
-- claims one atomically, and processes it. application_runs.status
-- values change from ('QUEUED','RUNNING','STOPPED','COMPLETE') to
-- ('PENDING','RUNNING','STOPPED','COMPLETE') -- QUEUED renamed to
-- PENDING to stop reading as "queued like an application" when it means
-- "written, not yet claimed by any worker".

alter table application_runs drop constraint application_runs_status_check;
alter table application_runs add constraint application_runs_status_check
    check (status in ('QUEUED', 'PENDING', 'RUNNING', 'STOPPED', 'COMPLETE'));

update application_runs set status = 'PENDING' where status = 'QUEUED';

alter table application_runs drop constraint application_runs_status_check;
alter table application_runs add constraint application_runs_status_check
    check (status in ('PENDING', 'RUNNING', 'STOPPED', 'COMPLETE'));
alter table application_runs alter column status set default 'PENDING';

-- claimed_by/claimed_at: which worker_id claimed this run and when --
-- separate from applications.worker_id, which is per-application.
-- stop_requested: set by the "Stop Run" button (Vercel or local) --
-- deliberately NOT the same as writing status='STOPPED' directly, which
-- would race with the daemon's own status writes on the row it's
-- actively processing. The daemon checks this flag between applications
-- and, if set, is the one that actually transitions status -> STOPPED.
alter table application_runs
    add column claimed_by     text,
    add column claimed_at     timestamptz,
    add column stop_requested boolean not null default false;

-- Atomic "claim one PENDING run" -- same FOR UPDATE SKIP LOCKED pattern
-- as claim_next_queued_application()/claim_next_queued_application_for_run(),
-- so exactly one daemon (even if more than one were ever running) claims
-- any given run.
create or replace function claim_next_pending_run(
    p_worker_id text
)
returns setof application_runs
language plpgsql
security definer
set search_path = public
as $$
begin
    return query
    update application_runs
    set status = 'RUNNING',
        claimed_by = p_worker_id,
        claimed_at = now(),
        updated_at = now()
    where id = (
        select id
        from application_runs
        where status = 'PENDING'
        order by created_at asc
        limit 1
        for update skip locked
    )
    returning *;
end;
$$;

revoke execute on function claim_next_pending_run(text) from public;
revoke execute on function claim_next_pending_run(text) from anon;
revoke execute on function claim_next_pending_run(text) from authenticated;
grant execute on function claim_next_pending_run(text) to service_role;

-- ── worker_heartbeats ────────────────────────────────────────────────────
-- One row per worker_id (in practice, one row -- a single Mac worker
-- daemon). Vercel reads the freshest heartbeat and compares its age to
-- decide ONLINE vs OFFLINE display; it never assumes a worker is
-- connected just because the web app itself is reachable.
create table worker_heartbeats (
    worker_id         text primary key,
    status             text not null default 'ONLINE'
                           check (status in ('ONLINE', 'BROWSER_DISCONNECTED')),
    last_heartbeat_at  timestamptz not null default now(),
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create trigger worker_heartbeats_set_updated_at
    before update on worker_heartbeats
    for each row execute function set_updated_at();

alter table worker_heartbeats enable row level security;
revoke all on worker_heartbeats from anon, authenticated;
