# Vertical Slice 01: Magnet Submission and Observable TorBox Queue

Planned for: 2026-08-07  
Status: Implemented; external Sonarr/Radarr and live TorBox smoke test pending  
Target: One focused development day

## Implementation result

Implemented on 2026-08-06 with:

- migration-managed async SQLite persistence;
- validated settings with secret redaction;
- hex and base32 magnet info-hash parsing;
- provider-neutral torrent contracts and a TorBox client;
- persistent submission, queued reconciliation, polling, retry, and restart
  recovery services;
- authenticated qBittorrent application, category, add, info, and properties
  endpoints; and
- 39 passing tests on Python 3.13 and in `python:3.12-slim`.

The remaining smoke test needs external credentials and services: connect live
Sonarr and Radarr instances, submit a controlled magnet with a TorBox API
token, and observe it reach `provider_ready`. No live provider call is part of
the automated suite.

## Outcome

At the end of this slice, Sonarr or Radarr can:

1. connect to Downloadarr as a qBittorrent client;
2. validate or create its category;
3. submit a magnet;
4. immediately see a persistent queue item identified by the torrent info hash;
5. observe that item transition as TorBox accepts and processes it; and
6. still see the same state after Downloadarr restarts.

This slice stops when TorBox reports that its files are ready. Downloading
those files to local storage belongs to Vertical Slice 02.

## Demonstration

The completion demonstration should follow this exact path:

```text
Sonarr/Radarr Test
        |
POST /api/v2/torrents/add (magnet + category)
        |
SQLite job committed immediately
        |
TorBox create torrent
        |
TorBox ID stored on the same job
        |
Periodic TorBox status polling
        |
GET /api/v2/torrents/info shows current state and progress
        |
Restart Downloadarr
        |
The same job resumes polling and remains visible
```

## Scope

### Included

- Install FastAPI, Uvicorn, SQLAlchemy 2.x async support, and `aiosqlite`.
- Add application configuration from environment variables.
- Create the initial SQLite schema and migration mechanism.
- Persist jobs, categories, and provider references.
- Parse and validate BitTorrent v1 `btih` magnet hashes.
- Implement the TorBox create, single-item list, and queued-list operations.
- Add a background polling service with bounded concurrency and backoff.
- Implement the minimum qBittorrent endpoints needed for connection testing,
  category validation, magnet submission, and queue observation.
- Recover unfinished jobs automatically after application restart.
- Unit, integration, and API contract tests using fake TorBox responses.
- One optional live TorBox smoke test gated by an environment variable.

### Explicitly excluded

- Binary `.torrent` upload and bencode parsing.
- TorBox file URL requests.
- Local file downloading.
- Multi-file path publication.
- Pause, resume, priority, and force-start controls.
- Deleting completed data.
- Dashboard or frontend work.
- Docker Compose integration.
- Provider cache preflight.
- Usenet and web-download support.

These exclusions prevent the first task from becoming a partial implementation
of several unrelated workflows.

## API surface

### Authentication and connection

| Method | Endpoint | Behavior |
|---|---|---|
| POST | `/api/v2/auth/login` | Validate configured username/password and issue `SID` |
| POST | `/api/v2/auth/logout` | Invalidate `SID` |
| GET | `/api/v2/app/webapiVersion` | Return `2.8.1` |
| GET | `/api/v2/app/version` | Return `v4.3.9` |
| GET | `/api/v2/app/preferences` | Return save path, DHT, and retention defaults |

Bearer authentication may be implemented alongside cookie authentication if
it does not delay the slice. Cookie login is required for the demonstration.

### Categories

| Method | Endpoint | Behavior |
|---|---|---|
| GET | `/api/v2/torrents/categories` | Return persisted category map |
| POST | `/api/v2/torrents/createCategory` | Create or idempotently accept a category |

Category records contain a unique name and save path. The default save path
comes from application configuration.

### Submission and queue

| Method | Endpoint | Behavior |
|---|---|---|
| POST | `/api/v2/torrents/add` | Accept one magnet and optional category |
| GET | `/api/v2/torrents/info` | List jobs, optionally filtered by category or hash |
| GET | `/api/v2/torrents/properties` | Confirm a job exists and return its save path |

`/torrents/add` must commit the job before scheduling provider work. Its HTTP
response must not wait for TorBox.

## Initial database model

Use migration-managed SQLite tables rather than `create_all()` at runtime.

### `jobs`

| Column | Type | Rules |
|---|---|---|
| `id` | UUID/text | Primary key |
| `info_hash` | text | Unique, lowercase, 40 hexadecimal characters |
| `name` | text/null | Magnet display name until TorBox supplies a canonical name |
| `category_id` | foreign key/null | Category at submission time |
| `source_uri` | text | Magnet; redact query details in logs |
| `state` | text | Validated internal state enum |
| `size` | integer/null | Provider-reported bytes |
| `progress` | real | Internal fraction from 0.0 to 1.0 |
| `download_speed` | integer | Bytes per second |
| `eta` | integer/null | Seconds |
| `error_code` | text/null | Stable machine-readable failure code |
| `error_message` | text/null | Sanitized operator-facing message |
| `created_at` | datetime | UTC |
| `updated_at` | datetime | UTC |
| `next_poll_at` | datetime/null | Durable scheduling timestamp |
| `poll_failures` | integer | Consecutive transient failures |

### `provider_jobs`

| Column | Type | Rules |
|---|---|---|
| `job_id` | foreign key | Unique, cascade on job removal |
| `provider` | text | Initially `torbox` |
| `remote_id` | integer/null | TorBox torrent ID |
| `queued_id` | integer/null | TorBox queued ID |
| `provider_state` | text/null | Last raw provider state |
| `payload` | JSON/text | Small sanitized provider metadata only |
| `last_polled_at` | datetime/null | UTC |

### `categories`

| Column | Type | Rules |
|---|---|---|
| `id` | UUID/text | Primary key |
| `name` | text | Unique, case-sensitive qBittorrent category name |
| `save_path` | text | Absolute container-visible path |
| `created_at` | datetime | UTC |
| `updated_at` | datetime | UTC |

Do not store API tokens, signed URLs, cookies, or full TorBox responses in the
database.

## Internal state machine

```text
submitted
    -> provider_queued
    -> provider_downloading
    -> provider_ready

Any non-terminal state
    -> retry_wait
    -> previous intended state

Any state
    -> failed
```

State changes occur in explicit service methods and within database
transactions. Route handlers and the TorBox HTTP client must not mutate model
state directly.

### qBittorrent projection

| Internal state | qBittorrent state |
|---|---|
| `submitted` | `metaDL` |
| `provider_queued` | `queuedDL` |
| `provider_downloading` | `downloading` |
| `retry_wait` | `stalledDL` |
| `provider_ready` | `queuedDL` |
| `failed` | `error` |

`provider_ready` remains `queuedDL`, not a completed upload state, because no
local files exist yet. This prevents Sonarr/Radarr from attempting a premature
import.

## Magnet ingestion

The parser must:

- require the `magnet:` scheme;
- locate an `xt=urn:btih:<hash>` value regardless of query ordering;
- accept 40-character hexadecimal hashes;
- optionally accept 32-character base32 hashes and normalize them to hex;
- reject missing, malformed, or unsupported hashes;
- extract `dn` only as an untrusted display-name hint;
- preserve the original magnet for the TorBox request;
- never write the complete magnet to normal logs because tracker parameters
  may be sensitive.

Duplicate submissions are idempotent by normalized info hash. Return a
successful qBittorrent response and retain the existing job.

## TorBox client

Create a provider-neutral protocol and a TorBox implementation. The job service
depends on the protocol rather than concrete HTTP calls.

```python
class TorrentProvider(Protocol):
    async def create_magnet(self, magnet: str) -> ProviderSubmission: ...
    async def get_torrent(self, remote_id: int) -> ProviderTorrent: ...
    async def get_queued(self) -> list[ProviderQueuedTorrent]: ...
```

### Required TorBox calls

1. `POST /v1/api/torrents/createtorrent`
   - multipart field `magnet`;
   - `allow_zip=false`;
   - `as_queued=true`;
   - `add_only_if_cached=false`.
2. `GET /v1/api/torrents/mylist?id=<remote_id>&bypass_cache=true`.
3. `GET /v1/api/queued/getqueued` for submissions that return only a queued
   identifier.

Set `Authorization: Bearer <token>` where required. Use one shared
`aiohttp.ClientSession`, explicit timeouts, bounded response sizes, and
structured error types.

### Error classification

| Condition | Classification |
|---|---|
| Timeout, connection reset | Transient |
| HTTP 429 | Transient; honor `Retry-After` |
| HTTP 500-599 | Transient |
| HTTP 401/403 | Terminal configuration/authentication failure |
| HTTP 400/422 | Terminal input or account failure unless documented otherwise |
| Malformed successful response | Transient for a bounded number of attempts, then failed |

Never expose the TorBox token or full authorization response in an exception.

## Polling service

The scheduler should use one process and asyncio tasks.

- Claim due jobs from SQLite using `next_poll_at`.
- Limit concurrent provider calls with a semaphore.
- Poll active jobs approximately every 5-10 seconds with jitter.
- Poll queued and retrying jobs every 30-60 seconds.
- Persist `next_poll_at` so restarts do not create a request burst.
- Back off transient failures exponentially with a configured maximum.
- Reset `poll_failures` after a successful provider response.
- On shutdown, stop accepting work, cancel the poll loop, and await in-flight
  calls before closing the database and HTTP session.

The poller must reconcile a job by info hash when a TorBox queued record turns
into a normal torrent record.

## Configuration

Downloadarr loads `config/settings.json` by default. Set `DOWNLOADARR_CONFIG`
to use another path. Environment variables override values in the JSON file,
which makes the same configuration suitable for local and container use.

Copy `settings.example.json` to `config/settings.json`; the real file is
ignored by Git because it contains secrets. Back up this file through the
future `/config` volume rather than committing it.

Supported environment variables:

```text
DOWNLOADARR_DATABASE_URL=sqlite+aiosqlite:///data/downloadarr.db
DOWNLOADARR_DOWNLOAD_PATH=/downloads
DOWNLOADARR_USERNAME=downloadarr
DOWNLOADARR_PASSWORD=<secret>
DOWNLOADARR_API_KEY=<optional bearer key>
TORBOX_API_TOKEN=<secret>
TORBOX_API_BASE=https://api.torbox.app/v1/api
TORBOX_REQUEST_TIMEOUT=30
DOWNLOADARR_PROVIDER_CONCURRENCY=4
```

Secrets must use `repr=False` or equivalent redaction in configuration models.
Startup should fail clearly when required configuration is absent or invalid.

## Suggested package layout

```text
src/downloadarr/
  api/
    app.py
    auth.py
    dependencies.py
    qbittorrent.py
    schemas.py
  db/
    engine.py
    models.py
    repositories.py
    migrations/
  jobs/
    service.py
    states.py
    poller.py
  magnets.py
  providers/
    base.py
    torbox.py
    models.py
  settings.py
```

The existing downloader package remains independent of FastAPI, SQLAlchemy,
and TorBox.

## Implementation plan

1. **Project dependencies and settings**
   - Add runtime and test dependencies.
   - Implement validated, redacted environment configuration.
2. **Database foundation**
   - Define models and the first migration.
   - Add repositories with transaction boundaries.
3. **Magnet parser**
   - Normalize hashes and names.
   - Add malformed and duplicate cases.
4. **Provider boundary and TorBox client**
   - Define provider DTOs and exceptions.
   - Implement create, get-one, and queued-list calls.
5. **Job service and poller**
   - Commit before provider submission.
   - Persist transitions, retry schedules, and provider identifiers.
   - Recover due jobs after restart.
6. **qBittorrent API facade**
   - Add auth, version, preferences, categories, add, info, and properties.
7. **Contract and restart tests**
   - Test through HTTP with a fake TorBox server and a real temporary SQLite
     database.
8. **Manual smoke test**
   - Start Downloadarr locally.
   - Run Sonarr/Radarr `Test`.
   - Submit one controlled magnet using a TorBox test account/token.
   - Confirm persistence and restart recovery without downloading files.

## Required tests

### Magnet tests

- Hex and base32 hashes normalize correctly.
- Query order, encoded names, and multiple `xt` values are handled.
- Missing and malformed `btih` values fail with a qBittorrent-compatible
  submission response.
- Duplicate hashes are idempotent.

### TorBox client tests

- Immediate torrent ID response.
- Queued ID response and later reconciliation.
- Downloading and ready responses.
- 401/403 terminal authentication errors.
- 429 with numeric and HTTP-date `Retry-After`.
- 5xx, timeout, invalid JSON, and unsuccessful response envelope.
- Token and magnet redaction in logs and exceptions.

### Persistence and scheduler tests

- Job exists before the provider coroutine runs.
- Each state transition survives a new database session.
- Restart resumes submitted, queued, downloading, and retrying jobs.
- Terminal jobs are not polled.
- Poll concurrency never exceeds configuration.
- Shutdown awaits in-flight provider operations.

### qBittorrent contract tests

- Login success, failure, cookie reuse, and logout.
- App version and preferences shapes.
- Category creation and listing.
- Magnet add returns success without waiting for TorBox.
- `/torrents/info` filters by category and hash.
- Progress is projected as `0.0-1.0` and byte fields are integers.
- `/torrents/properties` returns 404 for an unknown hash.
- `provider_ready` is not exposed as completed.

## Definition of done

This slice is complete only when:

- all new and existing tests pass on Python 3.13;
- all tests pass in `python:3.12-slim`;
- migrations create a fresh database and upgrade the previous revision;
- the API contains no TorBox-specific logic outside provider/job services;
- secrets and complete magnets do not appear in logs or errors;
- killing and restarting the process preserves and resumes an active job;
- Sonarr and Radarr connection tests succeed;
- a submitted magnet appears immediately and tracks TorBox to
  `provider_ready`; and
- Sonarr/Radarr do not attempt an import during this slice.

## Follow-up vertical slice

Vertical Slice 02 will begin at `provider_ready` and implement:

- persisted TorBox file records;
- safe multi-file path planning;
- signed URL providers using `/torrents/requestdl`;
- local file scheduling through the existing downloader;
- aggregate and per-file local progress;
- atomic torrent completion and qBittorrent completed states;
- `content_path` and `/torrents/files` import behavior.
