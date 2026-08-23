-- Phase 7.3: worker_heartbeats gains RECOVERING alongside ONLINE /
-- BROWSER_DISCONNECTED / AUTH_REQUIRED / SECURITY_CHALLENGE -- written
-- briefly by the worker daemon when a mid-run Playwright/CDP disconnect
-- is caught and being reconciled (dice_browser.worker_daemon's recovery
-- path, run_registry.reconcile_run_after_disconnect), so the frontend
-- can show "Worker: RECOVERING" truthfully instead of nothing changing
-- until the next successful poll overwrites it.

alter table worker_heartbeats drop constraint worker_heartbeats_status_check;
alter table worker_heartbeats add constraint worker_heartbeats_status_check
    check (status in ('ONLINE', 'BROWSER_DISCONNECTED', 'AUTH_REQUIRED', 'SECURITY_CHALLENGE', 'RECOVERING'));
