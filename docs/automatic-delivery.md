# Automatic TorBox Delivery

Downloadarr treats provider completion and local completion as separate states.
A torrent that is cached or finished in TorBox is not reported as complete to
Sonarr or Radarr until every selected file is safely published locally.

## Lifecycle

1. Poll TorBox until the torrent is download-ready.
2. Persist its file IDs, relative paths, and sizes in SQLite.
3. Validate every provider path before creating local directories.
4. Request a signed URL for each file and pass an async refresh callback to the
   resumable downloader.
5. Download to `.downloadarr.part`, checkpoint progress in the manifest, and
   atomically publish the final file.
6. Mark the job `completed` only after all persisted files are complete.

The connection setting remains eight for explicit generic parallel HTTP
transfers. `download.provider_max_connections` caps provider deliveries at
four connections by default to comply with TorBox's current recommendation.
Fresh `auto` transfers use a browser-style full GET; interrupted transfers can
continue with validated ranges. Set `download.transfer_mode` to `parallel` to
force dynamic segmented delivery up to the provider ceiling.

## qBittorrent facade

While local delivery is active, `/api/v2/torrents/info` returns `downloading`.
After local publication it returns `stalledUP`, a completion timestamp, and the
exact `content_path`. `/api/v2/torrents/files` reports persisted per-file names,
sizes, and progress.

`POST /api/v2/torrents/delete` removes the local job. With
`deleteFiles=false`, published data is preserved. With `deleteFiles=true`, only
validated file targets beneath the job's configured download root are removed.
Deletion is currently limited to completed or failed jobs; cancellation of an
active transfer will be added with explicit task ownership in a later slice.

## Restart behavior

File metadata and state survive process restarts in SQLite. Byte-level resume
state survives in each downloader manifest. A restarted job requests a fresh
signed URL and continues incomplete chunks rather than trusting an expired URL.

## Deferred storage contract

Category save paths and the default download root remain configurable. The
final container volume layout will be chosen after inspecting the existing
Sonarr, Radarr, and RDT Client mounts. Identical container paths are preferred,
but this delivery implementation does not hard-code them.
