-- The existing ordered-pair CHECK and UNIQUE constraints are retained.
CREATE INDEX IF NOT EXISTS dialogs_a_list_idx ON dialogs(participant_a, created_at DESC, dialog_id DESC);
CREATE INDEX IF NOT EXISTS dialogs_b_list_idx ON dialogs(participant_b, created_at DESC, dialog_id DESC);
