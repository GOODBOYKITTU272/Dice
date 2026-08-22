-- Phase 6.4: submission policy belongs to the run, not to whichever CLI
-- flag happened to be passed when the daemon was started. The daemon is
-- a long-running service that may claim many runs over its lifetime --
-- each one must carry its own, explicit, persisted policy so "daemon
-- started with --submission-policy AUTHORIZED_AUTONOMOUS" can never
-- silently become "every future run this daemon ever claims is
-- autonomous", including ones a human never reviewed for that.

alter table application_runs
    add column submission_policy text not null default 'AUTHORIZED_AUTONOMOUS'
        check (submission_policy in ('REQUIRE_CONFIRMATION', 'AUTHORIZED_AUTONOMOUS'));

-- Every row that already existed just received the new column's default
-- (AUTHORIZED_AUTONOMOUS) from the ADD COLUMN above. Explicitly backfill
-- those (and, within this one migration transaction, only those -- no
-- other transaction can insert a row between these two statements) back
-- to REQUIRE_CONFIRMATION: the actual historical default they ran under
-- (dice_browser.worker.SubmissionPolicy.REQUIRE_CONFIRMATION was the
-- only policy ever wired into the Jobs selection flow before this
-- migration). This preserves old runs' real behavior instead of
-- silently upgrading already-created, not-yet-processed runs to
-- autonomous submission a human never chose for them.
update application_runs set submission_policy = 'REQUIRE_CONFIRMATION';
