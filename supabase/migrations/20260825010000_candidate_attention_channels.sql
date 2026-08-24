-- Phase 7.5: durable candidate <-> messaging-channel identity, replacing
-- "assume the one DICEPILOT_CANDIDATE_ID env var owns every inbound
-- message" with a real, auditable binding -- the actual architecture
-- requirement (never TELEGRAM_CHAT_ID/IMESSAGE_PHONE as the permanent
-- identity model), and what makes "unknown sender" rejection possible
-- at all.
--
-- external_user_id is the provider-native identity: Telegram's numeric
-- chat id (as text) or iMessage's contact address (phone/email). The
-- unique constraint on (channel, external_user_id) is what makes a
-- channel identity structurally unable to map to two different
-- candidates -- a second bind attempt for an already-claimed identity
-- fails at the database, not just in application logic.
create table candidate_attention_channels (
    id               uuid primary key default gen_random_uuid(),
    candidate_id     uuid not null,
    channel          text not null check (channel in ('TELEGRAM', 'IMESSAGE')),
    external_user_id text not null,
    destination      text,
    verified_at      timestamptz,
    is_enabled       boolean not null default true,
    is_primary       boolean not null default false,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),

    constraint candidate_attention_channels_unique_identity unique (channel, external_user_id)
);

create index candidate_attention_channels_candidate_idx
    on candidate_attention_channels (candidate_id, channel);

create trigger candidate_attention_channels_set_updated_at
    before update on candidate_attention_channels
    for each row execute function set_updated_at();

alter table candidate_attention_channels enable row level security;
revoke all on candidate_attention_channels from anon, authenticated;

-- Short-lived, single-use linking codes -- the "/start ABCD1234" (Telegram)
-- / plain-text "ABCD1234" (iMessage) onboarding flow. A code is scoped to
-- exactly one candidate+channel at creation time; consuming it is what
-- actually creates/updates the candidate_attention_channels row.
create table attention_link_codes (
    code         text primary key,
    candidate_id uuid not null,
    channel      text not null check (channel in ('TELEGRAM', 'IMESSAGE')),
    expires_at   timestamptz not null,
    consumed_at  timestamptz,
    created_at   timestamptz not null default now()
);

alter table attention_link_codes enable row level security;
revoke all on attention_link_codes from anon, authenticated;
