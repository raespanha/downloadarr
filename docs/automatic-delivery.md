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

Sonarr and Radarr may submit either one magnet URI or one multipart `.torrent`
file. Downloadarr validates the torrent's bencoded structure, calculates its
v1 info hash from the original `info` dictionary bytes, and stores the upload
in SQLite before the poller submits it to TorBox. Version 1 and hybrid torrents
are supported; pure BitTorrent v2 torrents are rejected until the facade can
represent their 64-character hashes correctly.

While local delivery is active, `/api/v2/torrents/info` returns `downloading`.
After local publication it returns `pausedUP`, a completion timestamp, and the
exact `content_path`. `/api/v2/torrents/files` reports persisted per-file names,
sizes, and progress.

`POST /api/v2/torrents/delete` first marks each job as being removed, cancels
and awaits any active local transfer, and then deletes the exact TorBox torrent
or queued submission. With `deleteFiles=false`, local published and resumable
data is preserved. With `deleteFiles=true`, only validated final, partial, and
manifest targets beneath the job's configured download root are removed. A
failed provider cleanup preserves the SQLite job and resumable local state.

Completed jobs report `pausedUP`, allowing Arr applications to recognize a
stopped, import-ready item and apply their configured completed-download
cleanup behavior.

The full import and post-import removal contract can be checked with the
opt-in `downloadarr-verify-arr-cleanup` command documented in
`docs/live-arr-cleanup-test.md`. It is kept separate from routine tests because
the live variant observes real Arr and filesystem state.

## Restart behavior

File metadata and state survive process restarts in SQLite. Byte-level resume
state survives in each downloader manifest. A restarted job requests a fresh
signed URL and continues incomplete chunks rather than trusting an expired URL.

## Deferred storage contract

Category save paths and the default download root remain configurable. The
final container volume layout will be chosen after inspecting the existing
Sonarr, Radarr, and RDT Client mounts. Identical container paths are preferred,
but this delivery implementation does not hard-code them.
