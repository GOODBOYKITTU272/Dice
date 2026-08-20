-- Follow-up to 20260820175616_dicepilot_foundation.sql.
--
-- Closes the RPC-permission finding from Phase 1 live verification:
-- claim_next_queued_application()'s original `revoke ... from public` only
-- removed the implicit PUBLIC-pseudo-role grant. This project's existing
-- `ALTER DEFAULT PRIVILEGES ... GRANT ALL ON FUNCTIONS TO anon, authenticated`
-- grants those two roles their own direct EXECUTE at function-creation time,
-- independent of PUBLIC, so they were never actually removed. Revoking from
-- each role by name (the same pattern already used correctly for the table
-- grants in the foundation migration) closes that gap.
--
-- No table structure change. No change to application/queue behavior or to
-- the function body — same signature (uuid, text), same logic.

revoke execute on function public.claim_next_queued_application(uuid, text) from public;
revoke execute on function public.claim_next_queued_application(uuid, text) from anon;
revoke execute on function public.claim_next_queued_application(uuid, text) from authenticated;
grant execute on function public.claim_next_queued_application(uuid, text) to service_role;
