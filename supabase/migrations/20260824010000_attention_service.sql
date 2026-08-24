-- Phase 7.4: Apply/Skip messaging flow (Attention Service).
--
-- Two additions:
--
-- 1. applications.status gains AWAITING_USER_DECISION (a lightweight
--    precheck passed, job offer sent, waiting for the candidate to tap
--    Apply/Skip -- no wizard has been touched yet) and SKIPPED (terminal;
--    the candidate declined). The existing applications_candidate_job_
--    unique constraint means a SKIPPED row also naturally prevents the
--    same job from ever being re-offered, matching the product decision
--    ("do not surface same job again unless explicit future reset
--    policy exists") without any extra bookkeeping.
--
--    AWAITING_USER_DECISION -> QUEUED (Apply) deliberately reuses the
--    exact existing QUEUED status and claim mechanism -- Apply does not
--    invent a parallel "processing" concept, it just unlocks the queue
--    exactly the way the Jobs-selection UI's "Start Applications" button
--    already does today.
--
-- 2. attention_events: durable idempotency + audit log for outbound
--    messages (job offers, missing-question prompts, confirmations,
--    success/failure notices) and inbound actions (Apply/Skip/Confirm/
--    Edit/Answer), across both Telegram and iMessage. Two independent
--    idempotency guarantees this table exists to serve:
--      - outbound: never resend the same message_type for the same
--        application on the same channel (e.g. never re-offer a job
--        every worker poll cycle).
--      - inbound: never re-process the same provider-native event twice
--        (external_event_id), so a webhook/polling retry can't double-
--        apply an action.
create table attention_events (
    id               uuid primary key default gen_random_uuid(),
    application_id   uuid not null references applications(id) on delete cascade,
    candidate_id     uuid not null,
    channel          text not null check (channel in ('TELEGRAM', 'IMESSAGE')),
    direction        text not null check (direction in ('OUTBOUND', 'INBOUND')),
    message_type     text not null
                         check (message_type in (
                             'JOB_OFFER', 'MISSING_QUESTION', 'ANSWER_CONFIRMATION',
                             'SUBMISSION_SUCCESS', 'SUBMISSION_FAILURE'
                         )),
    action           text
                         check (action is null or action in ('APPLY', 'SKIP', 'CONFIRM', 'EDIT', 'ANSWER')),
    external_message_id text,
    payload          jsonb,
    created_at       timestamptz not null default now()
);

-- Outbound idempotency: at most one OUTBOUND row per
-- (application_id, channel, message_type) -- a job offer, or the success
-- notice, is sent at most once per application per channel. Missing-
-- question/confirmation prompts are NOT covered by this unique index
-- (deliberately -- see the partial index below) since the same
-- message_type legitimately repeats across different questions on the
-- same application.
create unique index attention_events_outbound_once_idx
    on attention_events (application_id, channel, message_type)
    where direction = 'OUTBOUND' and message_type in ('JOB_OFFER', 'SUBMISSION_SUCCESS', 'SUBMISSION_FAILURE');

-- Inbound idempotency: the same provider-native event id is never
-- processed twice, regardless of which application/channel it names --
-- a webhook/polling retry delivering the identical external_message_id
-- must be a pure no-op the second time.
create unique index attention_events_inbound_external_id_idx
    on attention_events (channel, external_message_id)
    where direction = 'INBOUND' and external_message_id is not null;

create index attention_events_application_idx
    on attention_events (application_id, direction, message_type);

alter table attention_events enable row level security;
revoke all on attention_events from anon, authenticated;

alter table applications drop constraint applications_status_check;
alter table applications add constraint applications_status_check
    check (status in (
        'AWAITING_USER_DECISION', 'SKIPPED',
        'QUEUED', 'PROCESSING', 'NEEDS_INPUT', 'SUBMITTING',
        'SUBMITTED', 'FAILED_RETRYABLE', 'FAILED'
    ));
