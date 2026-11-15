CREATE TABLE IF NOT EXISTS dialogs (
    dialog_id uuid PRIMARY KEY,
    participant_a uuid NOT NULL,
    participant_b uuid NOT NULL,
    last_seq bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dialogs_ordered_pair CHECK (participant_a < participant_b),
    CONSTRAINT dialogs_unique_pair UNIQUE (participant_a, participant_b)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id uuid PRIMARY KEY,
    dialog_id uuid NOT NULL REFERENCES dialogs(dialog_id),
    sender_id uuid NOT NULL,
    client_message_id uuid NOT NULL,
    seq bigint NOT NULL CHECK (seq > 0),
    text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 4000),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_dialog_sequence UNIQUE (dialog_id, seq),
    CONSTRAINT messages_idempotency UNIQUE (sender_id, client_message_id)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id bigserial PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    message_id uuid NOT NULL UNIQUE REFERENCES messages(message_id),
    event_type text NOT NULL CHECK (event_type = 'message.created'),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);

CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON outbox_events (next_attempt_at, id) WHERE published_at IS NULL;
