# Container deployment

For production use the complete [production runbook](production-runbook.md)
and `compose.production.yml`. This page covers the lightweight local setup.

## Image

Downloadarr uses Python 3.12 and runs as the non-root user `downloadarr`
(UID/GID 1000 by default). Override the `UID` and `GID` build arguments when
the shared storage has different ownership.

The image exposes port 6500 and checks `/readyz`. Configuration is read from
`/config/settings.json`. The build context excludes configuration, downloads,
manifests, media, tests, and Git metadata, so real secrets must never be copied
into an image layer.

## Compose

Create ignored local configuration from the tracked templates:

```bash
cp .env.example .env
mkdir -p config
cp settings.example.json config/settings.json
```

Replace all placeholders. Both files contain overlapping secrets for clarity;
environment values from `.env` take precedence over JSON values. Set
`DOWNLOADARR_MEDIA_PATH` to a host directory shared with Sonarr and Radarr:

```bash
DOWNLOADARR_MEDIA_PATH=/srv/downloads docker compose -f compose.example.yml up -d --build
```

The corresponding paths inside every container are:

```json
{
  "download": {
    "path": "/downloads",
    "categories": {
      "tv-sonarr": "/downloads/tv-sonarr",
      "radarr": "/downloads/radarr"
    }
  }
}
```

Sonarr and Radarr should use Downloadarr's Compose service name and port 6500,
the same credentials as `qbittorrent` in the Downloadarr settings, and their
matching category. Avoid remote path mappings when all containers already see
identical `/downloads` paths.

The example publishes only to `127.0.0.1`. On a shared Docker network Sonarr
and Radarr can connect directly to `downloadarr:6500` without publishing the
port. If another machine must connect, set `DOWNLOADARR_BIND` to a trusted LAN
address and protect access with network controls or an authenticated reverse
proxy.

## Migration from another client

Use a maintenance window and keep the previous client stopped but recoverable:

1. Back up the old client and Downloadarr `/config` directories.
2. Give Downloadarr the old service/DNS name if avoiding simultaneous Arr edits.
3. Mount the same staging directory at the same container path.
4. Configure matching categories and credentials.
5. Test the qBittorrent connection from both Arr applications.
6. Submit one small, legal, replaceable fixture and verify import and cleanup.
7. Remove stale remote path mappings only after the end-to-end test passes.

Do not delete the old client volume until rollback is no longer required.

## Secrets and backups

For development, `.env` and `config/settings.json` are ignored. For production,
prefer the `_FILE` secret variables shown in `compose.production.yml` and keep
secret files mode `0600`. Protect `/config` backups: they contain credentials
and SQLite job state.
