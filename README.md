# Downloadarr

Downloadarr is a self-hosted TorBox download client for Sonarr and Radarr. It
implements the qBittorrent Web API surface those applications need, persists
jobs in SQLite, downloads ready files with safe resume support, and provides an
authenticated monitoring dashboard.

> **Alpha:** the project has automated tests and has completed live Sonarr and
> Radarr import cycles, but it is not yet a stable release. Run one application
> process only, keep backups, and test with replaceable data first.

## What it provides

- qBittorrent-compatible authentication, torrent submission, polling, category,
  pause/resume and deletion endpoints for Sonarr and Radarr;
- TorBox magnet and `.torrent` submission with durable local job state;
- resumable HTTP delivery, integrity checks, bounded retries, and conservative
  handling of existing files;
- extension allowlisting and an unconditional executable/script denylist;
- lifecycle, transfer, failure and operational monitoring in the dashboard;
- atomic settings writes, backups, health/readiness endpoints, and maintenance
  commands.

## Quick start with Docker Compose

Requirements: Docker Compose, a TorBox API token, and a directory shared with
Sonarr/Radarr.

```bash
git clone https://github.com/raespanha/downloadarr.git
cd downloadarr
cp .env.example .env
mkdir -p config
cp settings.example.json config/settings.json
```

Replace every `replace-with-...` value in `.env` and
`config/settings.json`. Use the same Downloadarr username/password in the
Sonarr and Radarr qBittorrent client configuration. Then start the service:

```bash
DOWNLOADARR_MEDIA_PATH=/srv/downloads docker compose -f compose.example.yml up -d --build
```

Open `http://127.0.0.1:6500/` and sign in. The example binds only to localhost;
set `DOWNLOADARR_BIND` deliberately if another host must connect. Sonarr and
Radarr must see the same `/downloads` category paths as Downloadarr.

For a hardened deployment, immutable image policy, secret files, backup and
restore steps, see [Container deployment](docs/deployment.md) and the
[production runbook](docs/production-runbook.md).

## Configuration

The default configuration is `config/settings.json`. Environment variables
override JSON fields, and secret `_FILE` variants are supported for the
qBittorrent password/API key, TorBox token, and Sonarr/Radarr API keys.

The service intentionally refuses to start without a non-placeholder
qBittorrent-compatible password and a TorBox API token. Real configuration,
database files, backups, media and transfer metadata are ignored by Git and
excluded from the Docker build context.

See [settings documentation](docs/settings.md) for the schema and complete
environment-variable list.

Planned work and the boundary for a stable release are tracked in the
[roadmap](docs/roadmap.md).

## Development

Python 3.12 or 3.13 is supported. Dependencies are hash-locked.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-test.lock
PYTHONPATH=src python -m pytest -q
```

On PowerShell, activate with `.venv\Scripts\Activate.ps1` and run tests with
`$env:PYTHONPATH = "src"` followed by `python -m pytest -q`.

## Important limitations

- One process/worker only; multi-worker and replica scheduling are unsupported.
- SQLite must be on local storage, not a network filesystem.
- Docker inside unprivileged LXC is not a supported deployment target.
- A dashboard pause may be local-only when the provider cannot be paused.
- Downloadarr is not affiliated with TorBox, Sonarr, Radarr, or qBittorrent.

Use only content you are legally entitled to download. See
[SECURITY.md](SECURITY.md) for responsible disclosure and deployment guidance.

## License

[MIT](LICENSE)
