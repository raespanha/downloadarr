# Operational controls and recovery

Downloadarr persists operator intent separately from the provider/delivery phase. A job keeps its
workflow state (`provider_downloading`, `delivering`, and so on) and also has a control state:
`running`, `paused`, or `removing`. This prevents a restart or scheduler poll from accidentally
resuming a paused transfer.

## Pause and resume

The qBittorrent facade implements both the v4 names and v5 aliases:

- `POST /api/v2/torrents/pause` and `/resume`
- `POST /api/v2/torrents/stop` and `/start`
- `hashes` accepts one hash, pipe-separated hashes, or `all`; matching is case-insensitive.

An incomplete paused job reports `pausedDL`. Completed jobs remain `pausedUP`, so pausing cannot
make a completed Arr import ineligible.

Pause always stops Downloadarr scheduling and cancels/awaits an active local HTTP transfer. Its
partial file and manifest remain resumable. When TorBox is actively downloading a non-cached
torrent, Downloadarr also asks TorBox to pause it. Queued, cached, provider-ready, and local-delivery
jobs are paused locally only. The dashboard exposes `local` versus `local_and_provider` scope and
warns that TorBox may continue.

## Retry

Retry is distinct from resume. It is accepted only for `failed` and `retry_wait` jobs and derives
the recovery phase from durable data:

1. delivery rows resume local delivery;
2. a remote TorBox ID resumes provider polling;
3. a queued ID resumes queue polling;
4. only a job with no provider identity returns to submission.

Partial files, manifests, provider IDs, and delivery rows are retained. Automatic transient retries
are bounded by `scheduler.max_job_failures` (default `5`) before becoming a terminal failure.

## Restart and crash recovery

- Provider submission intent is committed before calling TorBox. An ambiguous restart reconciles by
  info hash/queued list before any second create.
- Publication writes a credential-free receipt before atomically renaming the completed file. If the
  process stops between publication and the history commit, the receipt validates recovery; an
  arbitrary same-sized destination is not trusted.
- Removal is a durable saga. The `removing` intent is stored before provider/local cleanup, and a
  failed cleanup is selected again after restart. Resume/retry cannot revive it.
- Controls cancel and await active writers before modifying or deleting files.

## Security and audit

Dashboard mutations require a cookie-authenticated session and a per-session CSRF token. qBittorrent
API authentication remains separate. Every accepted/no-op/warning control writes a durable audit
event with hash, service, indexer, actor, transition, outcome, and safe detail. Tokens and signed URLs
must never be written to these records.

Downloadarr remains a one-process/one-worker service. Cross-process leases are not implemented;
production must not run multiple Uvicorn workers against the same database.
