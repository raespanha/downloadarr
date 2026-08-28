# Container deployment

Downloadarr supports one deployment path: a Linux host running Docker Engine
and the Docker Compose plugin, using the repository's `Dockerfile` and
`compose.yml`. Windows and Docker Desktop are not currently supported.

## Container image

The image uses Python 3.12 and runs as the non-root `downloadarr` account. UID
and GID default to `1000`; set `DOWNLOADARR_UID` and `DOWNLOADARR_GID` before
building when shared storage has different ownership.

The image exposes port 6500, checks `/readyz`, and reads configuration from
`/config/settings.json`. The build context excludes configuration, downloads,
manifests, media, tests, and Git metadata. Real secrets must never be copied
into an image layer.

## Compose configuration

Create local configuration from the tracked templates:

```bash
cp .env.example .env
mkdir -p config
cp settings.example.json config/settings.json
docker network inspect downloadarr >/dev/null 2>&1 || docker network create downloadarr
```

Replace every `replace-with` value in `.env`. Set `DOWNLOADARR_MEDIA_PATH` to
the host directory shared with Sonarr and Radarr. Environment values override
the placeholder values in JSON.

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

The Compose service uses a read-only root filesystem, drops Linux capabilities,
enables `no-new-privileges`, limits processes/resources/log size, and grants
write access only through `/config`, `/downloads`, and the in-memory `/tmp`.

## Sonarr and Radarr connectivity

Attach Sonarr and Radarr to the external network named `downloadarr`, or to the
custom name set in `DOWNLOADARR_NETWORK`. Mount the same host staging directory
at `/downloads` in all three containers. Downloadarr's default category paths
are:

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

Configure both Arr applications to connect to `downloadarr:6500` with the
Downloadarr username/password and the matching category. Avoid Remote Path
Mappings when every container already sees the same `/downloads` path.

The dashboard binds to `127.0.0.1` by default. Containers on the shared network
can still use `downloadarr:6500`. If another machine must open the dashboard,
set `DOWNLOADARR_BIND` to a trusted LAN address and protect access with network
controls or an authenticated reverse proxy.

## Migration from another client

Use a maintenance window and keep the previous client stopped but recoverable:

1. Back up the old client and Downloadarr `/config` directories.
2. Mount the same staging directory at the same container path.
3. Configure matching categories and Downloadarr credentials in Arr.
4. Test the qBittorrent connection from both Arr applications.
5. Submit one small, legal, replaceable fixture and verify import and cleanup.
6. Remove stale Remote Path Mappings only after the end-to-end test passes.

Do not delete the old client volume until rollback is no longer required.

## Secrets and backups

`.env` and `config/settings.json` are ignored by Git. The application also
supports `_FILE` variants for sensitive environment values when an operator
wants to mount secret files. Keep those files mode `0600` and readable by the
configured container UID/GID.

Protect `/config` and its backups: they contain credentials and SQLite job
state. Follow the [production runbook](production-runbook.md) for verified
backup, restore, upgrade, and rollback procedures.
