-- The model each attempt ran on (the agent's configured model, or a per-event override such as the
-- Tech Lead's event_models). Older rows stay NULL; the token report falls back to the agent config.
ALTER TABLE execution_attempts ADD COLUMN model TEXT;
