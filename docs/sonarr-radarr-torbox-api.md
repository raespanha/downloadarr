# Sonarr, Radarr, and TorBox API Integration Specification

Last reviewed: 2026-08-06

Implementation tasks:

- [Vertical Slice 01: Magnet Submission and Observable TorBox Queue](tomorrow-vertical-01-magnet-queue.md)

## Purpose

Downloadarr will expose a focused qBittorrent Web API facade to Sonarr and
Radarr while using TorBox as the remote torrent provider and the Downloadarr
downloader for local file transfers.

```text
Sonarr / Radarr
       |
qBittorrent Web API facade
       |
Persistent Downloadarr jobs and scheduler
       |
TorBox torrent and file records
       |
Downloadarr file downloader
       |
Completed local torrent directory
```

The facade should implement the contract actually exercised by Sonarr and
Radarr. It does not need to reproduce the complete qBittorrent application.

## Reference implementations

- [qBittorrent Web API documentation](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-4.1%29)
- [Current Sonarr qBittorrent proxy](https://raw.githubusercontent.com/Sonarr/Sonarr/develop/src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrentProxyV2.cs)
- [Current Sonarr qBittorrent client](https://raw.githubusercontent.com/Sonarr/Sonarr/develop/src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrent.cs)
- [Current Radarr qBittorrent proxy](https://raw.githubusercontent.com/Radarr/Radarr/develop/src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrentProxyV2.cs)
- [Current Radarr qBittorrent client](https://raw.githubusercontent.com/Radarr/Radarr/develop/src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrent.cs)
- [TorBox OpenAPI schema](https://api.torbox.app/openapi.json)
- [TorBox Postman documentation](https://www.postman.com/torbox/torbox-api/documentation/b6l9hbv/main-api)
- [TorBox API rate limits](https://support.torbox.app/en/articles/13726368-api-rate-limits)

## qBittorrent compatibility profile

Downloadarr should initially advertise the following fixed versions:

```text
GET /api/v2/app/webapiVersion -> 2.8.1
GET /api/v2/app/version       -> v4.3.9
```

Web API 2.8.1 is new enough for `content_path`, modern torrent states,
category save paths, and seed limits at submission time. Downloadarr should
not advertise a newer API until its relevant behavior has been tested.

### Authentication

Support both authentication mechanisms used by current Servarr clients:

1. `POST /api/v2/auth/login` with form fields `username` and `password`.
   Successful login returns `Ok.` and an `SID` cookie. Invalid credentials
   return `Fails.` or HTTP 403.
2. `Authorization: Bearer <downloadarr-api-key>` on every API request.

Also provide `POST /api/v2/auth/logout` to invalidate an `SID` session.
Credentials, cookies, bearer tokens, and TorBox tokens must never be logged.

## Required qBittorrent endpoints

| Method | Endpoint | Responsibility |
|---|---|---|
| POST | `/api/v2/auth/login` | Establish an `SID` session |
| POST | `/api/v2/auth/logout` | Invalidate an `SID` session |
| GET | `/api/v2/app/webapiVersion` | Report compatibility version |
| GET | `/api/v2/app/version` | Report emulated qBittorrent version |
| GET | `/api/v2/app/preferences` | Return download and retention settings |
| GET | `/api/v2/torrents/info` | Return jobs for queue polling |
| POST | `/api/v2/torrents/add` | Accept a magnet or torrent file |
| POST | `/api/v2/torrents/delete` | Remove a job and optionally its files |
| GET | `/api/v2/torrents/properties` | Check existence and return the save path |
| GET | `/api/v2/torrents/files` | Return per-file names, sizes, and progress |
| GET | `/api/v2/torrents/categories` | Return configured categories |
| POST | `/api/v2/torrents/createCategory` | Create a category |
| POST | `/api/v2/torrents/setCategory` | Change a job category |

### Application preferences

The minimal response from `/api/v2/app/preferences` is:

```json
{
  "save_path": "/downloads",
  "dht": true,
  "max_ratio_enabled": false,
  "max_ratio": -1,
  "max_ratio_act": 0,
  "max_seeding_time_enabled": false,
  "max_seeding_time": -1
}
```

`dht` should be `true`. Sonarr and Radarr reject trackerless magnets when the
download client reports that DHT is disabled.

The save path is the path visible inside Downloadarr. Container deployments
may require a Sonarr/Radarr Remote Path Mapping when their visible path is
different.

### Adding torrents

`POST /api/v2/torrents/add` accepts `multipart/form-data` containing either:

- `urls`: a magnet or torrent URL; or
- `torrents`: uploaded binary `.torrent` data.

Accept and retain these optional fields where present:

- `category`
- `paused`
- `stopped`
- `ratioLimit`
- `seedingTimeLimit`
- `sequentialDownload`
- `firstLastPiecePrio`
- `contentLayout`

Unsupported BitTorrent-specific options may initially be stored or ignored,
but must not cause an otherwise valid submission to fail.

The endpoint returns an empty response or `Ok.`. It must return `Fails.` only
when the submission genuinely failed.

The local job must be committed before contacting or waiting for TorBox.
Sonarr and Radarr may query `/torrents/properties` for the new hash within
approximately one second.

The torrent info hash must be obtained locally:

- Parse `btih` from a magnet URI.
- For a `.torrent`, bdecode it and hash the exact raw bencoded `info`
  dictionary. Re-encoding a non-canonical dictionary is unsafe.
- Store lowercase hashes internally and compare case-insensitively.

### Torrent list response

`GET /api/v2/torrents/info` is the main polling endpoint. It must support the
optional `category` and `hashes` filters.

Each returned job should contain at least:

```json
{
  "hash": "0123456789abcdef0123456789abcdef01234567",
  "name": "Release.Name",
  "size": 1073741824,
  "progress": 0.62,
  "state": "downloading",
  "category": "sonarr",
  "save_path": "/downloads",
  "content_path": "/downloads/Release.Name",
  "amount_left": 408021893,
  "completed": 665719931,
  "dlspeed": 41943040,
  "upspeed": 0,
  "eta": 10,
  "ratio": 0,
  "added_on": 1785970000
}
```

Contract rules:

- `progress` is a fraction from `0.0` to `1.0`.
- Sizes and transfer speeds are integer bytes and bytes per second.
- Dates are Unix timestamps.
- `hash` is the torrent info hash, never the numeric TorBox ID.
- Completion means all requested files are safely published locally. Remote
  completion in TorBox is not sufficient.

### State mapping

| Downloadarr state | qBittorrent state | Servarr interpretation |
|---|---|---|
| Submitted to TorBox | `metaDL` | Waiting for metadata |
| Waiting for a TorBox slot | `queuedDL` | Queued |
| TorBox downloading | `downloading` | Downloading |
| Local file transfer | `downloading` | Downloading |
| Paused before completion | `pausedDL` | Paused |
| Recoverable provider problem | `stalledDL` | Warning |
| Terminal failure | `error` | Warning/failure handling |
| Local files published | `stalledUP` | Completed |
| Completed and stopped | `pausedUP` | Completed |

Avoid inventing custom qBittorrent state names. Unknown states default to
downloading in current Servarr clients and can conceal real failures.

### Completed paths

Because Downloadarr advertises Web API 2.8.1, Sonarr and Radarr use
`content_path` directly for completed jobs.

Single-file torrent:

```text
save_path:    /downloads
content_path: /downloads/Movie.2026.mkv
```

Multi-file torrent:

```text
save_path:    /downloads
content_path: /downloads/Release.Name
```

`content_path` must not equal `save_path`; current Servarr clients treat that
as a completed-path error.

### Torrent files response

`GET /api/v2/torrents/files?hash=<hash>` returns relative names with `/` path
separators on every operating system:

```json
[
  {
    "index": 0,
    "name": "Release.Name/Movie.mkv",
    "size": 1073242196,
    "progress": 1.0,
    "priority": 1,
    "is_seed": true,
    "availability": 1.0
  }
]
```

Per-file progress comes from the local downloader. Use priority `1` for files
being downloaded and `0` only for files deliberately excluded by a local
policy.

### Categories

Categories are local Downloadarr entities and are not sent to TorBox. Store:

- category name;
- category save path;
- creation and update timestamps.

Sonarr and Radarr commonly use separate `sonarr` and `radarr` categories.
They may also change a completed job to an imported category. Category filters
on `/torrents/info` must therefore be accurate.

### Deletion semantics

For `POST /api/v2/torrents/delete`:

`deleteFiles=false`:

- stop exposing the job to Sonarr/Radarr;
- optionally delete the remote TorBox item;
- preserve completed local files.

`deleteFiles=true`:

- cancel and await active local transfers;
- delete partial and completed local files;
- delete the TorBox item or queued submission;
- retain a small database tombstone/audit record.

All deletion targets must be resolved and verified to be inside a configured
download root.

## Optional qBittorrent endpoints

These endpoints are called only when related Servarr options are enabled or
when users manually control jobs:

| Endpoint | Initial behavior |
|---|---|
| `/api/v2/torrents/setShareLimits` | Store or accept ratio/time limits |
| `/api/v2/torrents/topPrio` | Raise local scheduler priority |
| `/api/v2/torrents/setForceStart` | Resume or prioritize a job |
| `/api/v2/torrents/pause` | Pause local and permitted TorBox work |
| `/api/v2/torrents/resume` | Resume a job |
| `/api/v2/torrents/editCategory` | Update a category path |
| `/api/v2/torrents/removeCategories` | Remove unused categories |

Do not claim successful support for `/torrents/setLocation` until moving a
partial or completed multi-file job is implemented safely.

## TorBox provider contract

The API base is:

```text
https://api.torbox.app/v1/api
```

Authenticated API calls use the TorBox API token.

### Required endpoints

| Method | Endpoint | Responsibility |
|---|---|---|
| POST | `/torrents/createtorrent` | Add a magnet or torrent file |
| GET | `/torrents/mylist?id=<id>` | Poll one torrent |
| GET | `/torrents/getqueued` | Reconcile queued submissions |
| POST | `/torrents/controltorrent` | Pause, resume, reannounce, or delete |
| POST | `/torrents/controlqueued` | Remove a queued submission |
| GET | `/torrents/requestdl` | Obtain a signed file URL |
| GET/POST | `/torrents/checkcached` | Optional cache preflight |

### Creating a torrent

`POST /torrents/createtorrent` accepts multipart fields including:

- `magnet`
- `file`
- `name`
- `seed`
- `allow_zip`
- `as_queued`
- `add_only_if_cached`

Recommended values:

```text
allow_zip=false
as_queued=true
add_only_if_cached=false
```

Download individual files rather than a generated ZIP. Individual transfers
preserve paths, support independent resume, avoid extraction, and use the
existing multi-connection downloader.

TorBox downloads all torrent files and does not expose a Real-Debrid-style
file-selection operation.

### Polling and readiness

`GET /torrents/mylist?id=<id>` provides fields including:

- `id`, `hash`, `name`, and `size`;
- `progress`, `download_state`, `download_speed`, and `eta`;
- `download_finished`, `download_present`, and `cached`;
- `files` and `download_path`;
- `created_at` and `updated_at`.

Each file includes fields such as `id`, `name`, `short_name`, `absolute_path`,
`size`, `hash`, `md5`, `mimetype`, and `infected`.

Use `download_finished` as the authoritative remote-readiness field. TorBox
explicitly warns clients not to use `download_state == "completed"` as the
completion condition. Reject or quarantine any file marked `infected`.

Normal `/mylist` responses may be cached for up to 600 seconds. A reasonable
polling policy is:

- active torrents: every 5-10 seconds with `bypass_cache=true`;
- queued or stalled torrents: every 30-60 seconds;
- add jitter so jobs do not synchronize their polls;
- respect HTTP 429 and `Retry-After`;
- use exponential backoff for transient 5xx and network failures.

TorBox currently documents a general limit of 300 requests per minute per
endpoint and API key. Uncached torrent creation also has a 60-per-hour limit.

### Requesting file download URLs

For each ready file:

```http
GET /v1/api/torrents/requestdl
    ?token=<api-key>
    &torrent_id=<torrent-id>
    &file_id=<file-id>
    &zip_link=false
    &redirect=false
```

The `data` response field contains a signed CDN URL. This maps directly to the
existing downloader URL provider:

```python
async def url_provider(refresh: bool) -> str:
    # Call requestdl initially and again whenever refresh is true.
    ...
```

Never persist signed CDN URLs. Persist only the TorBox torrent and file IDs,
then call `requestdl` when a transfer starts or refreshes.

Redact the following from every log, exception, and metric label:

- TorBox API tokens;
- `Authorization` headers;
- complete `requestdl` URLs;
- returned CDN URLs and their query strings.

## Persistent identifiers

Each job needs distinct identifiers rather than overloading one value:

| Identifier | Purpose |
|---|---|
| Torrent info hash | Public qBittorrent/Servarr download ID |
| Downloadarr job UUID | Stable internal job identity |
| TorBox torrent ID | Provider operations and polling |
| TorBox queued ID | Queued submission reconciliation |
| TorBox file ID | Signed URL requests |
| Local file ID | Progress and filesystem state |

The torrent info hash remains stable across provider deletion, URL refresh,
application restart, and local downloading.

## Recommended implementation sequence

1. Define SQLite jobs, files, categories, provider references, and state
   transitions.
2. Implement the TorBox client for create, poll, queued reconciliation,
   request-download, control, and delete operations.
3. Implement safe magnet and torrent info-hash extraction.
4. Add the multi-file local scheduler on top of the existing downloader.
5. Implement authentication, application, category, add, list, properties,
   files, and delete qBittorrent endpoints.
6. Add optional pause, resume, priority, force-start, and share-limit behavior.
7. Run contract tests using real Sonarr and Radarr containers against the
   Downloadarr facade.

## Acceptance scenarios

The first compatible vertical slice is complete when all of these scenarios
pass:

1. Sonarr and Radarr `Test` succeeds with cookie and bearer authentication.
2. Each application can create and validate its category.
3. A magnet submitted by either application appears by info hash immediately.
4. A binary `.torrent` upload produces the correct info hash and TorBox job.
5. Cached and uncached TorBox torrents transition through valid qBittorrent
   states.
6. A multi-file torrent is downloaded without flattening its directory tree.
7. Local progress, speed, remaining bytes, and ETA appear in the Servarr queue.
8. Completion is reported only after all local files are atomically published.
9. Sonarr/Radarr imports the exact `content_path` successfully.
10. Removing a job with `deleteFiles=false` preserves imported/downloaded data.
11. Removing with `deleteFiles=true` deletes only validated in-root targets.
12. Restarting Downloadarr recovers TorBox polling and local partial downloads.
13. Expired CDN URLs refresh without losing completed bytes.
14. TorBox 429, 5xx, timeout, and temporary outage responses remain bounded and
    visible without corrupting job state.
