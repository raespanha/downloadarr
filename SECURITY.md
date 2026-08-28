# Security Policy

## Supported versions

Downloadarr is pre-1.0 alpha software. Security fixes are made on the latest
revision of `main`; older revisions are not supported.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or include
tokens, signed URLs, magnets, media names, logs, or configuration files in an
issue. Use GitHub's **Security** tab and select **Report a vulnerability**.

Include a concise description, affected revision, reproduction steps, and the
impact. Redact credentials and personal paths. You should receive an initial
response within seven days.

If a credential may have been exposed, revoke or rotate it immediately.
Deleting it from a later commit does not remove it from Git history or forks.

## Deployment boundary

- Bind port 6500 to a trusted network or put it behind an authenticated reverse
  proxy. Do not expose Downloadarr directly to the Internet.
- Run exactly one Downloadarr process/Uvicorn worker. Multi-process scheduling
  and replicas are not supported.
- Keep `/config` and the download volume private and backed up. They may contain
  credentials, SQLite state, filenames, partial downloads, and resume metadata.
- Use a unique qBittorrent-compatible password of at least 12 characters and
  provide secrets through ignored configuration, environment variables, or
  mounted secret files.
- A local pause does not guarantee that TorBox pauses remote processing.

See [the production runbook](docs/production-runbook.md) for the complete
deployment controls and backup/restore procedure.
