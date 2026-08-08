# Downloadarr Settings

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
    "transfer_mode": "auto",
    "categories": {
      "tv-sonarr": "/downloads/tv-sonarr",
      "radarr": "/downloads/radarr"
    }
  },
  "qbittorrent": {
    "username": "downloadarr",
    "password": "secret",
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
| `transfer_mode` | string | `auto`, `sequential`, or `parallel`; `auto` uses a full GET for fresh files |
| `categories` | object | Category names mapped to container-visible save paths |

### qBittorrent facade

| Field | Type | Secret | Purpose |
|---|---|---|---|
| `username` | string | No | Sonarr/Radarr login username |
| `password` | string | Yes | Sonarr/Radarr login password |
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
| `provider_concurrency` | integer | Between 1 and 64 |
| `poll_interval` | number | Positive seconds for active jobs |
| `queued_poll_interval` | number | Positive seconds for queued jobs |
| `max_poll_backoff` | number | Positive retry ceiling in seconds |

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

Not every setting can safely change while jobs are active. The future UI should
classify updates as:

| Behavior | Fields |
|---|---|
| Apply immediately | Poll intervals, concurrency, provider timeout |
| Reconnect service | TorBox token/base URL, database-independent auth keys |
| Restart required | Database URL, download root while jobs are active |

The first settings API may conservatively mark every successful update as
requiring a restart. Hot reload should be introduced only with component-level
tests.

## Security model

Secrets are stored as plaintext in a file accessible to the Downloadarr
process. Encrypting them with a key stored beside the application would not
materially improve security. Protect the `/config` directory with operating
system permissions and encrypt backups at the storage layer.

Advanced deployments can keep secrets out of JSON entirely through environment
variables or future Docker secret-file support.
