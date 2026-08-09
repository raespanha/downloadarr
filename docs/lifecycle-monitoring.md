# Lifecycle monitoring, incidents, retention, and export

Downloadarr keeps an append-only lifecycle stream that survives operational job cleanup. Every
automatic phase change updates the job and inserts exactly one ordered event in the same SQLite
transaction. Preexisting jobs upgraded from schema 7 receive a `baseline_snapshot` marked as partial
history; Downloadarr does not invent time for phases that began before monitoring existed.

## Timing semantics

Phase duration is observed wall-clock time between Downloadarr state transitions. TorBox phases are
therefore bounded by polling observations, not exact provider timestamps. A pause is a separate
control overlay and is reported separately; wall phase time can include it. `retry_wait` is its own
phase. Per-file `transfer_history.elapsed` remains the actual local HTTP session time and must not be
treated as equal to the job-level delivery phase for multi-file releases.

After local completion, Downloadarr describes the state as awaiting client cleanup. A qBittorrent
delete request is recorded as `client_cleanup_requested`; it is not called a confirmed Arr import.
This matters because Sonarr/Radarr may delete duplicate-history or rejected releases too.

## Incidents and heartbeat

The monitor persists deduplicated incidents for terminal failures, repeated transient failures,
cleanup retries, and provider-control warnings. Repeated observations update one fingerprint rather
than creating a new alert row. An acknowledgment records operator review but does not resolve the
condition; the evaluator resolves it only after the underlying condition clears. Paused jobs never
generate stalled alerts.

`GET /ui/api/monitoring` returns the evaluator heartbeat, incident summary, observed phase timing,
and recent lifecycle events. The dashboard treats a heartbeat older than 90 seconds as stale, so an
empty alert list cannot silently masquerade as a healthy monitor.

## Segmentation and interpretation

Events and incidents snapshot the Sonarr/Radarr service and the Arr-reported indexer. Early `Unknown`
values are enriched when exact Arr history becomes available. Indexer charts show correlation only:
the indexer found the release, while TorBox and its CDN deliver the bytes.

Performance reports include sample files, delivered bytes, weighted average, median, p95, and peak.
Peak speed alone is not a service-health signal. Speed degradation alerting is intentionally deferred
until enough like-for-like large-transfer baseline data exists.

## Export

Authenticated exports are available at `GET /ui/api/export` with separate `lifecycle`, `transfers`,
`failures`, and `alerts` datasets; `format=json|csv`, range/service/indexer filters, and a bounded row
limit. The schema is versioned and uses bytes, bytes per second, and UTC ISO-8601 timestamps.

Identifiers are redacted by default. `include_identifiers=true` explicitly adds private hashes,
names, categories, paths, and safe error details where applicable. Exports never contain source URIs,
signed URLs, provider payloads, tokens, or credentials. CSV text that could start a spreadsheet
formula is prefixed safely. Responses use `Cache-Control: no-store` and attachment headers.

## Retention

`telemetry.retention_days` defaults to `0` (disabled). When enabled it must be 30–3650 days. Startup
pruning is isolated from downloader startup failures and deletes at most 1,000 eligible rows per
table per pass. Active-job lifecycle/control rows, unresolved failures, and open incidents are never
deleted. `GET /ui/api/retention` previews the next batch. SQLite `VACUUM` is deliberately not run in
the request or startup path.
