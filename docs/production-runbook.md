# Production runbook

## Supported baseline

Use one Downloadarr container in a Debian VM on Proxmox, with `/config` on local ext4, XFS,
or ZFS-backed storage. SQLite on NFS, CIFS/SMB, or FUSE is unsupported. Docker inside an
unprivileged LXC is an advanced deployment and is not the supported baseline; UID/GID and
mount mappings must be validated explicitly. Never use a privileged container merely to fix
permissions.

Downloadarr enforces its one-process architecture with an OS advisory lock next to SQLite.
A second process, Uvicorn worker, or replica exits before migration or polling. The lock file is
not deleted; kernel ownership makes a stale file harmless after a crash.

Use `compose.production.yml` with an immutable image tag/digest. It runs non-root, drops all
capabilities, uses a read-only root filesystem, constrains resources and logs, and binds the UI
to `127.0.0.1` by default. Put TLS and LAN authentication at a trusted reverse proxy. Do not
expose port 6500 directly to the Internet.

## Preflight and health

Run before first start and after changing mounts or ownership:

```console
downloadarr --config /config/settings.json doctor --json
downloadarr --config /config/settings.json doctor --online --json
```

Doctor output is redacted and checks settings, weak credentials, database integrity/schema,
filesystem type, free space, and atomic write/rename/fsync. Online mode adds DNS checks; it does
not modify TorBox or Arr.

`/healthz` is liveness only. `/readyz` requires the process lock, expected schema, a database
query, a live scheduler, and writable config/media roots with at least 64 MiB free. A TorBox
outage does not fail readiness and therefore cannot cause a provider-outage restart loop.

## Secrets

Production Compose uses `TORBOX_API_TOKEN_FILE`. The same `_FILE` convention is supported for
`DOWNLOADARR_PASSWORD`, `DOWNLOADARR_API_KEY`, and Sonarr/Radarr API keys. Keep secret files and
settings mode `0600`; environment values remain suitable only for development because container
metadata exposes them. Rotate any token that appeared in chat, shell history, or logs.

## Backup, verify, and restore

Back up to storage outside `/config`:

```console
downloadarr --config /config/settings.json backup /backups/downloadarr-2026-08-09
downloadarr backup-verify /backups/downloadarr-2026-08-09
```

Backup uses SQLite's online backup API, including committed WAL data. The versioned bundle has
the database, settings, checksums, and schema/app metadata. It may contain credentials, so protect
and encrypt it. It excludes media, `.part` files, manifests, and Arr/Plex libraries. Separately
snapshot staging if recovery of active local transfers is required.

Restore is offline, refuses while Downloadarr holds the lock, verifies checksums, settings,
schema, and SQLite integrity, and preserves a pre-restore recovery bundle:

```console
downloadarr --config /config/settings.json restore /backups/downloadarr-2026-08-09 --confirm RESTORE
```

An image rollback cannot undo a database migration. Roll back with both the earlier image and
its matching pre-upgrade database/settings backup. Measure RPO/RTO on the target machine.

## Upgrade and shutdown

1. Confirm there is no unintended active work and create/verify an online backup.
2. Pull the immutable versioned image and recreate exactly one service.
3. Require `/readyz`, then check schema, UI, Arr connection tests, and one legal smoke import.
4. Keep a 120-second stop grace period. SIGTERM marks readiness false, cancels and awaits active
   range tasks/checkpoints, closes provider/database resources, then releases the lock.

Never copy only a live `.db` in WAL mode or delete `-wal`/`-shm` files while running.
