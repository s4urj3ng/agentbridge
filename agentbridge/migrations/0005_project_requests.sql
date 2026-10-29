-- Projects added or reconfigured from the dashboard. The API process cannot write repositories
-- (systemd sandbox), so it queues the request and the engine clones the repo and registers it.
CREATE TABLE project_requests (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('create','update')),
    slug          TEXT NOT NULL,
    git_url       TEXT,
    manifest_json TEXT NOT NULL,
    first_goal    TEXT,
    status        TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','DONE','FAILED')),
    error         TEXT,
    requested_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    processed_at  TEXT
);
CREATE INDEX project_requests_status ON project_requests (status, id);
