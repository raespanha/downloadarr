# Downloadarr Settings

`download.minimum_file_size_mb` controls which TorBox files are delivered locally. Files smaller
than the configured number of MiB are skipped before a signed download URL is requested. `0`
keeps every file. The threshold applies when a newly ready torrent's file list is discovered; it
does not remove files from a transfer already in progress. If every file is below the threshold,
the job fails visibly instead of reporting a false successful download.

`download.allowed_file_extensions` is the primary safety boundary. Only files
whose final suffix appears in this list are selected for local delivery. The
default list contains common video formats. `download.blocked_file_extensions`
adds a configurable denylist. Executable and script formats such as `.exe`,
`.scr`, `.bat`, `.cmd`, `.msi`, `.ps1`, `.vbs`, and `.js` are always blocked,
even if the allowlist is empty or explicitly contains them. Filtering happens
before Downloadarr asks TorBox for a signed download URL.

Existing final files are handled conservatively. If the local size matches the
provider size, Downloadarr compares distributed byte samples (up to about 4
MiB per file) against the provider before reusing it. Files with different
sizes or samples are not overwritten or deleted. Legacy partial files such as
`.download` are not considered completed destinations.

## Goals

Downloadarr settings are designed to be:

- editable through a future dashboard;
- human-readable when troubleshooting;
- easy to back up and restore;
- validated before becoming active;
- safe from partial writes;
- overrideable through environment variables; and
- explicit about secret values.

Runtime job state remains in SQLite. User-controlled application settings live
in a versioned JSON document.

## File location

The default path is:

```text
config/settings.json
```

Set `DOWNLOADARR_CONFIG` to select another file. The real file and its backups
are ignored by Git. `settings.example.json` is the safe tracked template.

For containers, mount the entire directory:

```text
/config
```

Backing up `/config` preserves both settings and the SQLite database.

## Schema version 1

```json
{
  "schema_version": 1,
  "database": {
    "url": "sqlite+aiosqlite:////config/downloadarr.db"
  },
  "download": {
    "path": "/downloads",
    "connections": 8,
    "provider_max_connections": 4,
    "minimum_file_size_mb": 0,
    "allowed_file_extensions": [".mkv", ".mp4", ".avi"],
    "blocked_file_extensions": [],
    "transfer_mode": "auto",
    "categories": {
      "tv-sonarr": "/downloads/tv-sonarr",
      "radarr": "/downloads/radarr"
    }
  },
  "qbittorrent": {
    "username": "downloadarr",
    "password": "use-a-unique-password-here",
    "api_key": null,
    "webapi_version": "2.8.1",
    "application_version": "v4.3.9"
  },
  "torbox": {
    "api_token": "secret",
    "api_base": "https://api.torbox.app/v1/api",
    "request_timeout": 30
  },
  "integrations": {
    "sonarr": {"url": "http://sonarr:8989", "api_key": "secret", "category": "tv-sonarr"},
    "radarr": {"url": "http://radarr:7878", "api_key": "secret", "category": "radarr"}
  },
  "scheduler": {
    "provider_concurrency": 4,
    "poll_interval": 5,
    "queued_poll_interval": 30,
    "max_poll_backoff": 300
  }
}
```

Unknown fields are rejected. Unsupported schema versions are rejected rather
than silently interpreted using the wrong semantics.

## Sections

### Database

| Field | Type | Purpose |
|---|---|---|
| `url` | string | SQLAlchemy async database URL |

SQLite is the supported database for the initial release.
The four slashes in the container example make `/config/downloadarr.db` an
absolute path; three slashes would resolve `config/downloadarr.db` relative to
the image working directory and bypass the mounted `/config` directory.

### Download

| Field | Type | Rules |
|---|---|---|
| `path` | path | Local/container-visible completed download root |
| `connections` | integer | Between 1 and 256 |
| `provider_max_connections` | integer | Provider-specific connection ceiling; TorBox recommends 4 |
| `minimum_file_size_mb` | integer | Files below this size are skipped; `0` keeps every file |
| `allowed_file_extensions` | string list | Only these suffixes are delivered; empty allows any non-blocked suffix |
| `blocked_file_extensions` | string list | Additional suffixes to reject; executable/script formats are always rejected |
| `transfer_mode` | string | `auto`, `sequential`, or `parallel`; `auto` uses a full GET for fresh files |
| `categories` | object | Category names mapped to container-visible save paths |

### qBittorrent facade

| Field | Type | Secret | Purpose |
|---|---|---|---|
| `username` | string | No | Sonarr/Radarr login username |
| `password` | string | Yes | Required Sonarr/Radarr login password; at least 12 characters and not a known placeholder |
| `api_key` | string/null | Yes | Optional bearer authentication key |
| `webapi_version` | string | No | Advertised compatibility contract |
| `application_version` | string | No | Advertised qBittorrent version |

Compatibility versions should normally remain at their defaults. They are
defined here so the facade contract is explicit, not to encourage arbitrary
version changes from the UI.

### TorBox

| Field | Type | Secret | Purpose |
|---|---|---|---|
| `api_token` | string | Yes | User-owned TorBox API token |
| `api_base` | URL string | No | TorBox API base URL |
| `request_timeout` | number | No | Provider HTTP timeout in seconds |

### Scheduler

| Field | Type | Rules |
|---|---|---|
| `provider_concurrency` | integer | Between 1 and 64; shown in the UI as **Simultaneous downloads** |
| `poll_interval` | number | Positive seconds for active jobs |
| `queued_poll_interval` | number | Positive seconds for queued jobs |
| `max_poll_backoff` | number | Positive retry ceiling in seconds |

The scheduler continuously admits work up to `provider_concurrency`; it does
not wait for a long-running transfer to finish before discovering later jobs.
Each locally delivered file may independently use up to
`download.provider_max_connections` HTTP connections, so the approximate
maximum CDN connection count is the product of those two values. Reducing the
simultaneous-download limit does not cancel running jobs; it only delays new
admissions until the active count falls below the new limit.

### Sonarr and Radarr metadata enrichment

Each Arr integration has a base `url`, secret `api_key`, and the qBittorrent
`category` assigned to that service. Downloadarr uses these read-only API
credentials to find the grab history record matching a torrent info hash and
attribute performance and failures to the exact indexer. Either integration
may be left disabled by keeping its URL or API key empty.

## Environment overrides

Environment variables take precedence over the JSON document:

| Environment variable | Settings field |
|---|---|
| `DOWNLOADARR_DATABASE_URL` | `database.url` |
| `DOWNLOADARR_DOWNLOAD_PATH` | `download.path` |
| `DOWNLOADARR_CONNECTIONS` | `download.connections` |
| `DOWNLOADARR_PROVIDER_MAX_CONNECTIONS` | `download.provider_max_connections` |
| `DOWNLOADARR_ALLOWED_FILE_EXTENSIONS` | `download.allowed_file_extensions` |
| `DOWNLOADARR_BLOCKED_FILE_EXTENSIONS` | `download.blocked_file_extensions` |
| `DOWNLOADARR_TRANSFER_MODE` | `download.transfer_mode` |
| `DOWNLOADARR_USERNAME` | `qbittorrent.username` |
| `DOWNLOADARR_PASSWORD` | `qbittorrent.password` |
| `DOWNLOADARR_API_KEY` | `qbittorrent.api_key` |
| `TORBOX_API_TOKEN` | `torbox.api_token` |
| `TORBOX_API_BASE` | `torbox.api_base` |
| `TORBOX_REQUEST_TIMEOUT` | `torbox.request_timeout` |
| `DOWNLOADARR_SONARR_URL` | `integrations.sonarr.url` |
| `DOWNLOADARR_SONARR_API_KEY` | `integrations.sonarr.api_key` |
| `DOWNLOADARR_SONARR_CATEGORY` | `integrations.sonarr.category` |
| `DOWNLOADARR_RADARR_URL` | `integrations.radarr.url` |
| `DOWNLOADARR_RADARR_API_KEY` | `integrations.radarr.api_key` |
| `DOWNLOADARR_RADARR_CATEGORY` | `integrations.radarr.category` |
| `DOWNLOADARR_PROVIDER_CONCURRENCY` | `scheduler.provider_concurrency` |
| `DOWNLOADARR_POLL_INTERVAL` | `scheduler.poll_interval` |
| `DOWNLOADARR_QUEUED_POLL_INTERVAL` | `scheduler.queued_poll_interval` |
| `DOWNLOADARR_MAX_POLL_BACKOFF` | `scheduler.max_poll_backoff` |

The future UI must mark these fields as **managed by environment** and disable
editing them. Saving the UI must not copy environment-owned secrets into JSON.

## Persistence guarantees

`SettingsService` performs updates as follows:

1. Validate the complete candidate document.
2. Acquire the in-process settings lock.
3. Back up the current file with a UTC timestamp.
4. Write the new document to a uniquely named temporary file.
5. Flush and sync the temporary file.
6. Atomically replace `settings.json`.

Invalid candidates never modify the active file. Concurrent saves are
serialized.

## Secret behavior for the future API

Settings reads must return masked secrets:

```json
{
  "qbittorrent": {
    "password": "********",
    "api_key": null
  },
  "torbox": {
    "api_token": "********"
  }
}
```

Recommended update semantics:

- an omitted secret field retains the current secret;
- `"********"` retains the current secret;
- a non-empty replacement changes the secret;
- an explicit `null` clears an optional secret only;
- required secrets cannot be cleared while their integration is enabled.

The settings API must never return a saved plaintext secret after accepting
it. Logs, validation errors, telemetry, and audit records must also use masked
values.

## Reload behavior

Dashboard updates are validated, written atomically, and hot-applied. Active
HTTP transfers retain the Downloader instance and open files with which they
started; later files and jobs use the new values. The current behavior is:

| Behavior | Fields |
|---|---|
| Apply immediately | Download connections/mode, file-size and extension filters, simultaneous downloads, categories, paths, telemetry limits, Arr metadata settings, and future TorBox requests after token rotation |
| Existing transfer remains unchanged | The currently open local HTTP transfer |
| Restart required | Database URL and other startup-only settings not exposed by the dashboard |

## Security model

Secrets are stored as plaintext in a file accessible to the Downloadarr
process. Encrypting them with a key stored beside the application would not
materially improve security. Protect the `/config` directory with operating
system permissions and encrypt backups at the storage layer.

Deployments can keep secrets out of JSON through environment variables or the
implemented `_FILE` variants. The file variables are
`DOWNLOADARR_PASSWORD_FILE`, `DOWNLOADARR_API_KEY_FILE`,
`TORBOX_API_TOKEN_FILE`, `DOWNLOADARR_SONARR_API_KEY_FILE`, and
`DOWNLOADARR_RADARR_API_KEY_FILE`.

## Telemetry settings

`telemetry.retention_days` is `0` by default, which keeps history indefinitely. A nonzero value must
be 30–3650 days. `telemetry.export_max_rows` bounds authenticated JSON/CSV exports. Environment
overrides are `DOWNLOADARR_TELEMETRY_RETENTION_DAYS` and `DOWNLOADARR_EXPORT_MAX_ROWS`.
