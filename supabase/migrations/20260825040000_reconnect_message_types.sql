-- Phase 8D: two new outbound message types for the Dice reconnect flow.
-- RECONNECT_REQUIRED is sent at most once per application+channel (same
-- "outbound once" index every other terminal-ish notification uses) --
-- the explicit fix for "must not send repeated reconnect notifications
-- every worker poll" (Phase 8D spec). RECONNECT_SUCCESS is the
-- "continuing your application" message sent once reconnect_dice()
-- resumes a specific interrupted application.
alter table attention_events drop constraint attention_events_message_type_check;
alter table attention_events add constraint attention_events_message_type_check
    check (message_type in (
        'JOB_OFFER', 'MISSING_QUESTION', 'ANSWER_CONFIRMATION',
        'SUBMISSION_SUCCESS', 'SUBMISSION_FAILURE',
        'APPLY_ACK', 'SKIP_ACK', 'ANSWER_ACCEPTED', 'READY_TO_SUBMIT',
        'RECONNECT_REQUIRED', 'RECONNECT_SUCCESS'
    ));

drop index attention_events_outbound_once_idx;
create unique index attention_events_outbound_once_idx
    on attention_events (application_id, channel, message_type)
    where direction = 'OUTBOUND' and message_type in (
        'JOB_OFFER', 'SUBMISSION_SUCCESS', 'SUBMISSION_FAILURE',
        'APPLY_ACK', 'SKIP_ACK', 'READY_TO_SUBMIT',
        'RECONNECT_REQUIRED', 'RECONNECT_SUCCESS'
    );
