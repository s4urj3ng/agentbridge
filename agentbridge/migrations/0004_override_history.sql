-- Undo stack for runtime overrides (Telegram ↩️ Hoàn tác): the full set of overrides of one
-- project/agent as it was before each change.
CREATE TABLE override_history (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL,
    target      TEXT NOT NULL,
    before_json TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX override_history_target ON override_history (scope, target, id);
