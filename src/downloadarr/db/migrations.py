MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE categories (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    save_path TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE TABLE jobs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    info_hash VARCHAR(40) NOT NULL UNIQUE,
    name TEXT,
    category_id VARCHAR(36) REFERENCES categories(id),
    source_uri TEXT NOT NULL,
    state VARCHAR(32) NOT NULL,
    size INTEGER,
    progress FLOAT NOT NULL,
    download_speed INTEGER NOT NULL,
    eta INTEGER,
    error_code VARCHAR(64),
    error_message TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    next_poll_at DATETIME,
    poll_failures INTEGER NOT NULL
);
CREATE INDEX ix_jobs_info_hash ON jobs(info_hash);
CREATE INDEX ix_jobs_state ON jobs(state);
CREATE INDEX ix_jobs_next_poll_at ON jobs(next_poll_at);
CREATE TABLE provider_jobs (
    job_id VARCHAR(36) NOT NULL PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    remote_id INTEGER,
    queued_id INTEGER,
    provider_state VARCHAR(64),
    payload TEXT NOT NULL,
    last_polled_at DATETIME
);
CREATE INDEX ix_provider_jobs_remote_id ON provider_jobs(remote_id);
CREATE INDEX ix_provider_jobs_queued_id ON provider_jobs(queued_id);
""",
    2: """
ALTER TABLE jobs ADD COLUMN completed_at DATETIME;
CREATE TABLE delivery_files (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    provider_file_id INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    downloaded INTEGER NOT NULL,
    state VARCHAR(32) NOT NULL,
    error_message TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (job_id, provider_file_id)
);
CREATE INDEX ix_delivery_files_job_id ON delivery_files(job_id);
""",
    3: """
ALTER TABLE jobs ADD COLUMN source_kind VARCHAR(16) NOT NULL DEFAULT 'magnet';
ALTER TABLE jobs ADD COLUMN source_data BLOB;
""",
    4: """
CREATE TABLE transfer_history (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL,
    provider_file_id INTEGER NOT NULL,
    info_hash VARCHAR(40) NOT NULL,
    name TEXT NOT NULL,
    category VARCHAR(255) NOT NULL,
    relative_path TEXT NOT NULL,
    provider VARCHAR(32) NOT NULL,
    remote_id INTEGER,
    status VARCHAR(32) NOT NULL,
    total_bytes INTEGER NOT NULL,
    transferred_bytes INTEGER NOT NULL,
    elapsed FLOAT NOT NULL,
    average_speed INTEGER NOT NULL,
    peak_speed INTEGER NOT NULL,
    connections INTEGER NOT NULL,
    used_ranges INTEGER NOT NULL,
    range_requests INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    resumed INTEGER NOT NULL,
    cdn_host VARCHAR(255),
    started_at DATETIME NOT NULL,
    completed_at DATETIME NOT NULL,
    UNIQUE (job_id, provider_file_id)
);
CREATE INDEX idx_transfer_history_completed_at ON transfer_history(completed_at);
CREATE INDEX idx_transfer_history_info_hash ON transfer_history(info_hash);
""",
    5: """
ALTER TABLE jobs ADD COLUMN source_service VARCHAR(32) NOT NULL DEFAULT 'other';
ALTER TABLE jobs ADD COLUMN source_indexer VARCHAR(255);
ALTER TABLE jobs ADD COLUMN source_indexer_id INTEGER;
ALTER TABLE jobs ADD COLUMN source_metadata_checked_at DATETIME;
ALTER TABLE transfer_history ADD COLUMN service VARCHAR(32) NOT NULL DEFAULT 'other';
ALTER TABLE transfer_history ADD COLUMN indexer VARCHAR(255) NOT NULL DEFAULT 'Unknown';
ALTER TABLE transfer_history ADD COLUMN indexer_id INTEGER;
CREATE INDEX idx_transfer_history_completed_service
ON transfer_history(completed_at, service);
CREATE INDEX idx_transfer_history_completed_indexer
ON transfer_history(completed_at, indexer);
CREATE TABLE failure_events (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL,
    info_hash VARCHAR(40) NOT NULL,
    name TEXT NOT NULL,
    category VARCHAR(255) NOT NULL,
    service VARCHAR(32) NOT NULL,
    indexer VARCHAR(255) NOT NULL,
    stage VARCHAR(32) NOT NULL,
    error_code VARCHAR(64) NOT NULL,
    error_message TEXT NOT NULL,
    transient INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    bytes_downloaded INTEGER NOT NULL,
    occurred_at DATETIME NOT NULL,
    resolved_at DATETIME
);
CREATE INDEX idx_failure_events_occurred_at ON failure_events(occurred_at);
CREATE INDEX idx_failure_events_occurred_service
ON failure_events(occurred_at, service);
CREATE INDEX idx_failure_events_occurred_indexer
ON failure_events(occurred_at, indexer);
""",
    6: """
CREATE INDEX idx_transfer_history_service_completed
ON transfer_history(service, completed_at);
CREATE INDEX idx_transfer_history_indexer_completed
ON transfer_history(indexer, completed_at);
CREATE INDEX idx_failure_events_service_occurred
ON failure_events(service, occurred_at);
CREATE INDEX idx_failure_events_indexer_occurred
ON failure_events(indexer, occurred_at);
""",
    7: """
ALTER TABLE jobs ADD COLUMN control_state VARCHAR(16) NOT NULL DEFAULT 'running';
ALTER TABLE jobs ADD COLUMN paused_at DATETIME;
ALTER TABLE jobs ADD COLUMN control_scope VARCHAR(32);
ALTER TABLE jobs ADD COLUMN control_error TEXT;
ALTER TABLE jobs ADD COLUMN remove_delete_files INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN remote_cleanup_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN local_cleanup_done INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_jobs_control_next_poll ON jobs(control_state, next_poll_at);
CREATE TABLE control_events (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL,
    info_hash VARCHAR(40) NOT NULL,
    service VARCHAR(32) NOT NULL,
    indexer VARCHAR(255) NOT NULL,
    command VARCHAR(32) NOT NULL,
    actor VARCHAR(32) NOT NULL,
    from_state VARCHAR(32) NOT NULL,
    to_state VARCHAR(32) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    detail TEXT,
    occurred_at DATETIME NOT NULL
);
CREATE INDEX idx_control_events_hash ON control_events(info_hash);
CREATE INDEX idx_control_events_occurred_at ON control_events(occurred_at);
""",
    8: """
ALTER TABLE jobs ADD COLUMN phase_started_at DATETIME;
ALTER TABLE jobs ADD COLUMN transition_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN cleanup_failures INTEGER NOT NULL DEFAULT 0;
CREATE TABLE lifecycle_events (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    event_id VARCHAR(36) NOT NULL UNIQUE,
    event_key VARCHAR(128) NOT NULL UNIQUE,
    job_id VARCHAR(36) NOT NULL,
    generation INTEGER NOT NULL,
    info_hash VARCHAR(40) NOT NULL,
    name TEXT NOT NULL,
    category VARCHAR(255) NOT NULL,
    service VARCHAR(32) NOT NULL,
    indexer VARCHAR(255) NOT NULL,
    indexer_id INTEGER,
    provider VARCHAR(32) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    from_phase VARCHAR(32),
    to_phase VARCHAR(32),
    outcome VARCHAR(32) NOT NULL,
    code VARCHAR(64),
    detail TEXT,
    progress FLOAT NOT NULL,
    bytes_downloaded INTEGER NOT NULL,
    duration_seconds FLOAT,
    partial_history INTEGER NOT NULL,
    occurred_at DATETIME NOT NULL,
    recorded_at DATETIME NOT NULL
);
CREATE INDEX idx_lifecycle_job_sequence ON lifecycle_events(job_id, sequence);
CREATE INDEX idx_lifecycle_occurred_service ON lifecycle_events(occurred_at, service);
CREATE INDEX idx_lifecycle_occurred_indexer ON lifecycle_events(occurred_at, indexer);
CREATE INDEX idx_lifecycle_type_occurred ON lifecycle_events(event_type, occurred_at);
CREATE TABLE alert_instances (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    fingerprint VARCHAR(255) NOT NULL UNIQUE,
    rule VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    job_id VARCHAR(36),
    info_hash VARCHAR(40),
    service VARCHAR(32) NOT NULL,
    indexer VARCHAR(255) NOT NULL,
    summary TEXT NOT NULL,
    action TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    first_seen_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    acknowledged_at DATETIME,
    resolved_at DATETIME
);
CREATE INDEX idx_alert_status_last_seen ON alert_instances(status, last_seen_at);
CREATE INDEX idx_alert_service_last_seen ON alert_instances(service, last_seen_at);
CREATE TABLE monitor_status (
    id INTEGER NOT NULL PRIMARY KEY,
    last_evaluated_at DATETIME,
    last_pruned_at DATETIME,
    last_error TEXT
);
INSERT INTO monitor_status(id) VALUES (1);
"""
}
