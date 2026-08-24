-- Phase 7.5b: visible Apply/Skip/Confirm acknowledgements over Telegram/
-- iMessage. Four new message_type values -- APPLY_ACK, SKIP_ACK,
-- READY_TO_SUBMIT are each sent at most once per application+channel
-- (added to the existing outbound-once partial unique index, same
-- pattern as JOB_OFFER/SUBMISSION_SUCCESS/SUBMISSION_FAILURE).
-- ANSWER_ACCEPTED ("Got it") deliberately is NOT added to that index --
-- like MISSING_QUESTION/ANSWER_CONFIRMATION, it legitimately repeats
-- once per confirmed question on the same application.
alter table attention_events drop constraint attention_events_message_type_check;
alter table attention_events add constraint attention_events_message_type_check
    check (message_type in (
        'JOB_OFFER', 'MISSING_QUESTION', 'ANSWER_CONFIRMATION',
        'SUBMISSION_SUCCESS', 'SUBMISSION_FAILURE',
        'APPLY_ACK', 'SKIP_ACK', 'ANSWER_ACCEPTED', 'READY_TO_SUBMIT'
    ));

drop index attention_events_outbound_once_idx;
create unique index attention_events_outbound_once_idx
    on attention_events (application_id, channel, message_type)
    where direction = 'OUTBOUND' and message_type in (
        'JOB_OFFER', 'SUBMISSION_SUCCESS', 'SUBMISSION_FAILURE',
        'APPLY_ACK', 'SKIP_ACK', 'READY_TO_SUBMIT'
    );
