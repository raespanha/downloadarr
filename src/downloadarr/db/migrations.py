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
"""
}
