# Current MediaStack Integration Audit

Audit date: 2026-08-06; deployment status updated 2026-08-07. This document
records the Docker Desktop stack and the compatibility checks used while
Downloadarr replaced the TorBox RDT Client service.

## Docker topology

The Compose project is `mediastack` and its services use the
`mediastack_default` network. Sonarr and Radarr are running; the TorBox RDT
Client container is stopped.

Relevant services:

| Service | Address used by Arr | Host port | State during audit |
|---|---|---:|---|
| `rdt-client` | `rdt-client:6500` | 6500 | Downloadarr, running and healthy |
| `rdt-client-rd` | `rdt-client-rd:6500` | 6501 | Not running |
| `sonarr` | `sonarr:8989` | 8989 | Running |
| `radarr` | `radarr:7878` | 7878 | Running |

The existing RDT named volume is `mediastack_rdtclient_data`. It contains the
old RDT database and an Ubuntu test download. It must not be deleted as part of
the Downloadarr migration.

## Existing mounts

The useful shared contract already exists:

```text
Windows host                         Container
C:\torbox_media                      /torbox
C:\plex_media\series                 /series   (Sonarr/Plex/Bazarr)
C:\plex_media\movies                 /movies   (Radarr/Plex/Bazarr)
```

`C:\torbox_media` is mounted read-write at `/torbox` in RDT Client, Sonarr,
Radarr, Plex, and Bazarr. The Sonarr and Radarr application processes run as
UID/GID 1000 (`abc:users`) and both can read and write `/torbox`.

Downloadarr should therefore mount:

```yaml
volumes:
  - ./downloadarr/config:/config
  - C:\torbox_media:/torbox
```

Its container setting `download.path` should be `/torbox`. Category-specific
paths should be `/torbox/tv-sonarr` and `/torbox/radarr`.

## Sonarr settings

Enabled download client:

| Setting | Current value |
|---|---|
| Implementation | qBittorrent |
| Host | `rdt-client` |
| Port | `6500` |
| Category | `tv-sonarr` |
| Remove completed | Enabled |
| Remove failed | Enabled |
| Completed Download Handling | Enabled |

Sonarr has an identity Remote Path Mapping from `/torbox/` to `/torbox/` for
host `rdt-client`. It is unnecessary when both applications use the same path,
but it is harmless and can remain during the first migration test.

The current Sonarr root folder is `/torbox/series/`. Sonarr also has `/series`
mounted from `C:\plex_media\series`, but it is not the configured root folder.
This means the present series library is kept inside the shared TorBox staging
tree. Changing the library root is a separate media-migration decision and is
not required to connect Downloadarr.

## Radarr settings

Enabled download client:

| Setting | Current value |
|---|---|
| Implementation | qBittorrent |
| Host | `rdt-client` |
| Port | `6500` |
| Category | `radarr` |
| Remove completed | Enabled |
| Remove failed | Enabled |

The current Radarr root folder is `/movies/`, backed by
`C:\plex_media\movies`.

Radarr has a Remote Path Mapping from `C:\torbox_media\` to
`/torbox_movies/`. `/torbox_movies` is not mounted in the Radarr container, so
this mapping is inconsistent with the current Compose mounts. Downloadarr
reports `/torbox/...` paths and Radarr already sees that path directly. The
mapping does not match Downloadarr's reported remote prefix, so it is not
currently applied. Remove it later as configuration cleanup rather than making
it part of the first import test.

## Lowest-risk Downloadarr migration

Keep the existing Arr connection address and replace the stopped service in
place:

1. Build a Downloadarr container from `./downloadarr`.
2. Retain the Compose service/DNS name `rdt-client` and host port 6500.
3. Mount `./downloadarr/config` at `/config`.
4. Mount `C:\torbox_media` at `/torbox` read-write.
5. Set Downloadarr's default path to `/torbox`.
6. Create category `tv-sonarr` with save path `/torbox/tv-sonarr`.
7. Create category `radarr` with save path `/torbox/radarr`.
8. Match the qBittorrent username/password already configured in Sonarr and
   Radarr, or update both clients during the same maintenance window.
9. Remove Radarr's stale Remote Path Mapping after Downloadarr's test response
   confirms a `/torbox/...` `content_path`.
10. Test the qBittorrent handshake before submitting a legal test magnet.

Keeping the DNS name `rdt-client` avoids simultaneous edits to both Arr
applications. The old named volume remains detached and recoverable for
rollback.

## Current integration status

The production image, health check, ignored JSON settings, category bootstrap,
and qBittorrent handshake are deployed. Magnet submissions and multipart
`.torrent` uploads are persisted before TorBox submission, including enough
source data to recover after a process restart. Downloadarr accepts the
optional priority, force-start, share-limit, and post-import category calls
made by Arr. These controls are compatibility no-ops because TorBox does not
seed or expose a qBittorrent-style queue; the original category remains fixed
so the reported path cannot diverge from the physical delivery path.

On 2026-08-07 the live Sonarr and Radarr `testall` calls both completed with no
failures against Downloadarr at `rdt-client:6500`. Database migration 3 was
applied and the service remained healthy. No torrent or media job was
submitted during those checks.

Remaining before calling the vertical complete:

- perform one controlled legal download and completed-import test in Sonarr or
  Radarr;
- verify Arr removes the completed Downloadarr job while preserving or moving
  the imported library file as configured; and
- add structured cancellation before allowing active-job deletion.

The first end-to-end import test should use a small, explicitly approved legal
media fixture that Arr can identify. The Debian fixture remains useful for raw
TorBox/download stress testing, but cannot prove Sonarr or Radarr importing.
