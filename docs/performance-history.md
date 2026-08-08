# Performance History

Downloadarr stores compact transfer telemetry separately from operational jobs.
Arr may therefore remove an imported job, its TorBox torrent, and staging files
without erasing the measurements needed to monitor CDN and downloader health.

## Recorded measurements

One history row is written for each successfully delivered file:

- torrent info hash, display name, category, and relative file path;
- originating service (`sonarr`, `radarr`, or `other`) and indexer;
- provider and TorBox remote ID;
- total file bytes and bytes transferred during the current session;
- transfer duration, weighted average speed, and peak speed;
- configured connections actually available to the transfer;
- range mode, range request count, retry count, and resume status;
- final CDN hostname; and
- transfer start and completion timestamps.

Signed download URLs, TorBox tokens, and qBittorrent credentials are never
stored in performance history or completion logs.

Average speed is calculated from session bytes divided by transfer time. The
dashboard combines multiple files using the same byte/time weighting, so small
metadata files cannot distort a large video transfer. Peak speed uses a rolling
three-second sample to suppress short scheduling spikes.

## Dashboard

The authenticated dashboard provides:

- 7-day, 30-day, and all-time filters;
- average and peak speed timelines;
- delivered bytes, download count, and retry totals; and
- a recent-transfer table with size, speeds, connections, and retries.
- service and indexer filters and comparison summaries; and
- persistent failure events, their pipeline stage, and recovered/open status.

The authenticated JSON source is `GET /ui/api/performance?range=7d`. Optional
`service` and `indexer` query parameters filter the report (use `all` for no
filter). The range
may be `7d`, `30d`, or `all`. Seven- and thirty-day views use daily buckets;
the all-time view uses monthly buckets.

History begins with the first transfer completed after database migration 4.
Older completed jobs cannot be backfilled accurately because their download
timings were intentionally removed during Arr cleanup.

## Logs and retention

Every completed file emits a structured `transfer_completed` log line with the
same safe measurements. Provider, metadata, and delivery failures emit a
structured `transfer_failed` line and are also persisted in `failure_events`.
Transient events are marked recovered after a later successful processing
cycle; terminal failures remain open for monitoring. These records deliberately
survive Arr cleanup, just like successful transfer history.

SQLite history is retained indefinitely for now. Each
row is small, and indexes cover time-range and info-hash lookups. A configurable
retention policy can be added if long-running installations accumulate enough
history to justify one.

## Source attribution

The qBittorrent add request identifies the configured category but does not
include the originating indexer's name. Downloadarr therefore derives the
service from the category and optionally queries the matching Sonarr/Radarr
history record by exact torrent info hash. If the Arr integration is disabled
or no matching grab is available, the service remains known from the category
and the indexer is recorded as `Unknown`; tracker domains are not guessed.
