-- Phase 7.1: cloud worker migration. worker_heartbeats.status previously
-- only distinguished ONLINE / BROWSER_DISCONNECTED -- not enough to show
-- truthful state on the frontend once the worker is a cloud service the
-- user can't just look at. Extends it to also carry AUTH_REQUIRED and
-- SECURITY_CHALLENGE, written by the daemon's own periodic auth check
-- (dice_browser.worker_daemon._check_browser_and_auth), so the Vercel UI
-- can show "Dice Login Required" or "Security Challenge" truthfully
-- instead of just ONLINE/OFFLINE.

alter table worker_heartbeats drop constraint worker_heartbeats_status_check;
alter table worker_heartbeats add constraint worker_heartbeats_status_check
    check (status in ('ONLINE', 'BROWSER_DISCONNECTED', 'AUTH_REQUIRED', 'SECURITY_CHALLENGE'));
