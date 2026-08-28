# qBittorrent API specification (v4.3.2 / Web API 2.7)

This compatibility surface is based on the RDT-Client implementation in
`server/RdtClient.Web/Controllers/QBittorrentController.cs`. It describes the
qBittorrent endpoints used by Sonarr and Radarr.

## Required endpoints

### Authentication

- `POST /api/v2/auth/login` — accepts `username` and `password` form fields;
  returns `Ok.` or `Fails.` as plain text.
- `POST /api/v2/auth/logout` — ends the current session.

### Application handshake

- `GET /api/v2/app/version` — returns `v4.3.2`.
- `GET /api/v2/app/webapiVersion` — returns `2.7`.
- `GET /api/v2/app/buildInfo` — returns compatible library version metadata.
- `GET /api/v2/app/preferences` — returns preferences; `save_path`,
  `temp_path`, and `web_ui_username` reflect real configuration.
- `GET /api/v2/app/defaultSavePath` — returns the default download path.

### Torrent polling

- `GET /api/v2/torrents/info?category=X` — returns `TorrentInfo` objects with
  hash, name, size, progress, speeds, state, category, paths, remaining bytes,
  and timestamps. Progress combines provider and local delivery progress.
- `GET /api/v2/torrents/files?hash=X` — returns selected torrent files.
- `GET /api/v2/torrents/properties?hash=X` — returns size, downloaded bytes,
  speeds, piece counts, and elapsed-time details.

### Torrent commands

- `POST /api/v2/torrents/add` — accepts newline-separated magnet/HTTP URLs,
  category, and priority. Returns `Fails.` if the provider rejects a request.
- `POST /api/v2/torrents/delete` — accepts pipe-separated hashes and
  `deleteFiles`.
- `POST /api/v2/torrents/pause` — accepts hashes.
- `POST /api/v2/torrents/resume` — accepts hashes.
- `POST /api/v2/torrents/topPrio` — accepts hashes and sets maximum priority.

### Categories

- `GET /api/v2/torrents/categories` — returns category objects by name.
- `POST /api/v2/torrents/createCategory` — accepts a category name.
- `POST /api/v2/torrents/removeCategories` — accepts newline-separated names.
- `POST /api/v2/torrents/setCategory` — accepts hashes and a category.

### Transfer and synchronization

- `GET /api/v2/transfer/info` — returns connection status, downloaded bytes,
  current speed, and the rate limit.
- `GET /api/v2/sync/maindata` — returns a complete categories, torrents, and
  server-state snapshot. `full_update` is always true; incremental updates are
  not supported.

## No-op compatibility endpoints

These endpoints return a successful response so compatible clients can finish
initialization:

- `POST /api/v2/app/shutdown`
- `POST /api/v2/app/setPreferences`
- `POST /api/v2/torrents/setShareLimits`
- `POST /api/v2/torrents/filePrio`
- `POST /api/v2/torrents/createTags`
- `GET /api/v2/torrents/tags` — returns `[]`.

## State mapping

The first matching condition wins:

1. A failed job maps to `error`.
2. A completed job maps to `pausedUP`, which Arr treats as ready to import.
3. Provider delivery without seeders maps to `stalledDL`.
4. All other active work maps to `downloading`.

Downloadarr only exposes the distinctions required by Sonarr and Radarr; it
does not reproduce every state from a native qBittorrent client.

## Path construction

| Scenario | `save_path` | `content_path` |
| --- | --- | --- |
| No category, multiple files | `/downloads` | `/downloads/TorrentName/` |
| `tv-sonarr` category | `/downloads/tv-sonarr` | `/downloads/tv-sonarr/TorrentName/` |
| `radarr` category, one file | `/downloads/radarr` | `/downloads/radarr/TorrentName/` |

## Out of scope for the initial release

- watch folders;
- multiple simultaneous debrid providers;
- upload and seed-ratio behavior;
- symlink downloaders or rclone mounts;
- RAR extraction (provider files are expected to be ready for import).
