-- AgentBridge initial schema.
-- All timestamps are UTC ISO-8601 strings. JSON columns hold validated JSON text.

CREATE TABLE projects (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    repo_path       TEXT NOT NULL,              -- local clone used as the worktree source
    remote_url      TEXT,
    remote_name     TEXT NOT NULL DEFAULT 'origin',
    base_branch     TEXT NOT NULL DEFAULT 'main',
    manifest_json   TEXT NOT NULL DEFAULT '{}', -- parsed project manifest (policies, validation commands)
    paused          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- Persistent structured project context (charter, ADRs, verified facts ...).
CREATE TABLE project_context (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    kind        TEXT NOT NULL CHECK (kind IN ('charter','goal','architecture_decision','blocker','verified_fact','outstanding_pr','constraint','note')),
    content     TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_context_project ON project_context(project_id, kind, active);

CREATE TABLE milestones (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    key             TEXT NOT NULL,
    goal            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','COMPLETED','BLOCKED','CANCELLED','AWAITING_APPROVAL')),
    integration_branch TEXT,
    base_ref        TEXT,                       -- commit sha the milestone started from
    summary         TEXT,
    pr_url          TEXT,
    pr_number       INTEGER,
    tl_invocations  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    UNIQUE (project_id, key)
);

CREATE TABLE agents (
    id                  TEXT PRIMARY KEY,       -- identity name, e.g. claude-team-lead
    provider            TEXT NOT NULL,          -- claude | codex | gemini | antigravity | mindlogic | mock
    roles_json          TEXT NOT NULL DEFAULT '[]',
    enabled             INTEGER NOT NULL DEFAULT 1,
    paused              INTEGER NOT NULL DEFAULT 0,
    max_concurrency     INTEGER NOT NULL DEFAULT 1,
    daily_invocation_limit  INTEGER,
    weekly_invocation_limit INTEGER,
    reserved_review_invocations INTEGER NOT NULL DEFAULT 0,
    cooldown_until      TEXT,
    cooldown_reason     TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    circuit_open_until  TEXT,
    quota_error_streak  INTEGER NOT NULL DEFAULT 0,
    config_json         TEXT NOT NULL DEFAULT '{}',
    capability_json     TEXT NOT NULL DEFAULT '{}',
    last_detected_at    TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE tasks (
    id                  INTEGER PRIMARY KEY,
    project_id          INTEGER NOT NULL REFERENCES projects(id),
    milestone_id        INTEGER NOT NULL REFERENCES milestones(id),
    parent_task_id      INTEGER REFERENCES tasks(id),
    key                 TEXT NOT NULL,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'code' CHECK (kind IN ('code','research','test','review')),
    acceptance_json     TEXT NOT NULL DEFAULT '[]',
    validation_json     TEXT NOT NULL DEFAULT '[]', -- names of manifest validation commands
    allowed_paths_json  TEXT NOT NULL DEFAULT '[]', -- glob patterns; empty = any non-forbidden path
    assignee            TEXT REFERENCES agents(id),
    reviewer            TEXT REFERENCES agents(id),
    reviews_task_id     INTEGER REFERENCES tasks(id),
    state               TEXT NOT NULL DEFAULT 'CREATED',
    depth               INTEGER NOT NULL DEFAULT 0,
    revision_count      INTEGER NOT NULL DEFAULT 0,
    validation_retries  INTEGER NOT NULL DEFAULT 0,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    quota_requeues      INTEGER NOT NULL DEFAULT 0,
    timeout_seconds     INTEGER NOT NULL DEFAULT 1800,
    feedback            TEXT,                   -- latest revision feedback (from TL/reviewer/validation)
    worker_summary      TEXT,
    branch              TEXT,
    worktree_path       TEXT,
    head_sha            TEXT,
    base_sha            TEXT,
    pr_url              TEXT,
    pr_number           INTEGER,
    blocked_reason      TEXT,
    claim_token         TEXT,
    state_version       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (milestone_id, key)
);
CREATE INDEX idx_tasks_state ON tasks(state);
CREATE INDEX idx_tasks_milestone ON tasks(milestone_id);

CREATE TABLE task_dependencies (
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    depends_on_id   INTEGER NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_id),
    CHECK (task_id != depends_on_id)
);

CREATE TABLE task_transitions (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE execution_attempts (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER REFERENCES tasks(id),
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    purpose         TEXT NOT NULL CHECK (purpose IN ('work','review','tech_lead')),
    status          TEXT NOT NULL DEFAULT 'STARTING' CHECK (status IN ('STARTING','RUNNING','EXITED','COLLECTED','LOST','KILLED','TIMED_OUT')),
    pid             INTEGER,
    nonce           TEXT NOT NULL,
    run_dir         TEXT NOT NULL,          -- artifact dir holding prompt, stdout, stderr, exit.json
    cwd             TEXT,
    started_at      TEXT NOT NULL,
    deadline_at     TEXT NOT NULL,
    ended_at        TEXT,
    exit_code       INTEGER,
    duration_s      REAL,
    outcome         TEXT,                   -- success | failed | blocked | rate_limited | usage_limited | auth | timeout | lost | malformed
    failure_detail  TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd_reported REAL,
    event_id        INTEGER                 -- for tech_lead attempts: triggering event
);
CREATE INDEX idx_attempts_status ON execution_attempts(status);

CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER REFERENCES tasks(id),
    attempt_id  INTEGER REFERENCES execution_attempts(id),
    kind        TEXT NOT NULL,              -- stdout | stderr | diff | validation_log | prompt | result | review
    path        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    summary     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE validation_runs (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    attempt_id  INTEGER REFERENCES execution_attempts(id),
    passed      INTEGER NOT NULL,
    results_json TEXT NOT NULL,             -- per-command {name, argv, exit_code, duration, artifact}
    policy_violations_json TEXT NOT NULL DEFAULT '[]',
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);

CREATE TABLE reviews (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    reviewer    TEXT NOT NULL,              -- agent id, 'github:<login>' or 'human'
    source      TEXT NOT NULL CHECK (source IN ('agent','github','human','tech_lead')),
    verdict     TEXT NOT NULL CHECK (verdict IN ('approve','changes_requested','comment')),
    body        TEXT NOT NULL,
    external_id TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE git_refs (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    task_id     INTEGER REFERENCES tasks(id),
    milestone_id INTEGER REFERENCES milestones(id),
    ref_name    TEXT NOT NULL,
    sha         TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- base | task_head | integration | pr_head
    created_at  TEXT NOT NULL
);

CREATE TABLE worktree_leases (
    path        TEXT PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    agent_id    TEXT NOT NULL,
    leased_at   TEXT NOT NULL
);

CREATE TABLE decisions (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    milestone_id INTEGER REFERENCES milestones(id),
    event_id    INTEGER,
    attempt_id  INTEGER REFERENCES execution_attempts(id),
    decision_type TEXT,
    payload_json TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('applied','rejected','malformed')),
    rejection_reason TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE quota_usage (
    id          INTEGER PRIMARY KEY,
    agent_id    TEXT NOT NULL REFERENCES agents(id),
    attempt_id  INTEGER REFERENCES execution_attempts(id),
    purpose     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_s  REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    outcome     TEXT,
    retry_after_s INTEGER
);
CREATE INDEX idx_quota_agent_time ON quota_usage(agent_id, started_at);

-- Durable event queue. dedupe_key makes event creation idempotent.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    milestone_id INTEGER REFERENCES milestones(id),
    task_id     INTEGER REFERENCES tasks(id),
    type        TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key  TEXT UNIQUE,
    status      TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PROCESSING','DONE','FAILED','DEFERRED')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  TEXT,
    claimed_by  TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX idx_events_status ON events(status, id);

-- Append-only audit/activity log (everything shown on the dashboard timeline).
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    project_id  INTEGER,
    task_id     INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);

CREATE TABLE approval_requests (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    milestone_id INTEGER REFERENCES milestones(id),
    task_id     INTEGER REFERENCES tasks(id),
    action      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
    resolved_by TEXT,
    resolution_note TEXT,
    dedupe_key  TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
