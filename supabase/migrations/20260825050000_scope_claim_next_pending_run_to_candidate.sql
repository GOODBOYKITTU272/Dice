-- Phase 8D cleanup: claim_next_pending_run() previously claimed the
-- globally-oldest PENDING run with zero candidate scoping -- meaning
-- ANY worker process could claim ANY candidate's run. Masked in V1
-- (one real candidate in practice) but a genuine correctness gap the
-- moment a second candidate exists, and the exact reason the real
-- production dice-worker was silently claiming test-created PENDING
-- runs mid-test-suite (test_stop_run_route_sets_stop_requested and
-- friends), causing nondeterministic failures unrelated to any actual
-- code bug. Scoped the same way claim_next_queued_application()
-- already is -- application_runs.candidate_id already exists, no new
-- column needed.
create or replace function claim_next_pending_run(
    p_worker_id text,
    p_candidate_id uuid
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
          and candidate_id = p_candidate_id
        order by created_at asc
        limit 1
        for update skip locked
    )
    returning *;
end;
$$;

revoke execute on function claim_next_pending_run(text, uuid) from public;
grant execute on function claim_next_pending_run(text, uuid) to service_role;

-- The old text-only-arg signature is superseded; drop it so nothing can
-- silently keep calling the unscoped version.
drop function if exists claim_next_pending_run(text);
