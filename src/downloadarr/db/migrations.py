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
"""
}
