-- Owner runtime overrides (set from Telegram "Phân công"). They take precedence over the
-- YAML config and project manifests and survive sync_agents() and restarts.
-- scope 'project': target = project slug, key in (default_worker, workers, reviewers)
-- scope 'agent':   target = agent id,     key in (fallback_to, enabled, max_concurrency)
CREATE TABLE overrides (
    scope       TEXT NOT NULL,
    target      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (scope, target, key)
);
