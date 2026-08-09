# Container Deployment

For production use the full [production runbook](production-runbook.md) and hardened
`compose.production.yml`. This page is the lightweight local-development setup.

## Image

Downloadarr uses Python 3.12 and runs as the non-root user
`downloadarr` (UID/GID 1000 by default). Override the `UID` and `GID` build
arguments when the shared storage uses different ownership.

The image exposes port 6500 and includes an HTTP health check against
`/healthz`. Configuration is read from `/config/settings.json`.

The Docker build context excludes `config`, downloads, manifests, media, tests,
and Git metadata. A real settings file or TorBox token must never be copied into
an image layer.

## Compose

Copy `.env.example` to the ignored `.env` file and set `TORBOX_API_TOKEN`
before starting the service. The Compose example injects this file into the
container; environment settings override the JSON settings file.

`compose.example.yml` is a standalone template. Set `DOWNLOADARR_MEDIA_PATH`
to the host directory shared with Sonarr and Radarr:

```powershell
$env:DOWNLOADARR_MEDIA_PATH = 'C:\torbox_media'
docker compose -f compose.example.yml up -d --build
```

The corresponding settings use container paths:

```json
{
  "download": {
    "path": "/torbox",
    "connections": 8,
    "provider_max_connections": 4,
    "transfer_mode": "auto",
    "categories": {
      "tv-sonarr": "/torbox/tv-sonarr",
      "radarr": "/torbox/radarr"
    }
  }
}
```

Paths are stored as strings so a settings file written on Windows preserves
Linux container paths exactly.

`auto` uses a normal full GET for a fresh transfer and validated ranges after
an interruption. Set `transfer_mode` to `parallel` for an explicit segmented
transfer. The measured local Docker Desktop deployment uses `parallel` because
four dynamic ranges outperform its constrained single HTTP stream; native
Windows testing is faster with `auto`.

## Existing MediaStack migration

The local MediaStack keeps the Compose service name `rdt-client`. This retains
the address already configured in Sonarr and Radarr:

```text
rdt-client:6500
```

Only that stopped service is replaced. The former
`mediastack_rdtclient_data` named volume remains detached and recoverable. It
is not mounted into Downloadarr and must not be deleted during rollback testing.

The live 2026-08-06 validation confirmed:

- the container becomes healthy;
- Sonarr and Radarr resolve `rdt-client` over `mediastack_default`;
- both applications authenticate successfully;
- qBittorrent preferences and Web API version handshakes succeed;
- both configured categories are visible; and
- category-filtered torrent polling succeeds.

No media job was submitted as part of the connection handshake.

The subsequent Debian live stress test is recorded in
`docs/live-stress-test.md`. It confirmed persistent delivery and scoped cleanup
and exposed several edge cases that were fixed before normal Arr usage.

## Secrets and backups

`SettingsService` stores real secrets in the protected local JSON file and its
backups. API/display serialization remains masked. The `/config` directory and
its backups are excluded from both Git and Docker build context.

Back up `/config` securely: it contains the TorBox token, qBittorrent password,
and SQLite job state.
