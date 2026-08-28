# Automated Arr import and cleanup test

This opt-in live verifier checks the final Sonarr/Radarr integration boundary:

1. the selected job exists in Downloadarr;
2. Arr records `downloadFolderImported` for the same torrent info hash;
3. Arr registers the imported file with the expected size;
4. Arr removes the completed job through Downloadarr's qBittorrent facade; and
5. optional path maps confirm that the library file remains while staging is
   removed.

The verifier is passive: it never searches for, grabs, or submits a release.
Use a small, explicitly approved legal fixture. A genuine import changes
external state, so this command is never run in normal tests or CI.

## Run it

Install the package on Linux and supply process-scoped environment variables.
Keep real values out of Git, screenshots, shell history, and test reports.

```bash
export DOWNLOADARR_E2E_ARR_URL="http://127.0.0.1:8989"
export DOWNLOADARR_E2E_ARR_API_KEY="<arr-api-key>"
export DOWNLOADARR_E2E_DOWNLOADARR_URL="http://127.0.0.1:6500"
export DOWNLOADARR_E2E_USERNAME="<downloadarr-user>"
export DOWNLOADARR_E2E_PASSWORD="<downloadarr-password>"

downloadarr-verify-arr-cleanup \
  --arr sonarr \
  --hash <40-character-torrent-info-hash> \
  --timeout 7200 \
  --path-map "/downloads=/srv/media/downloads" \
  --path-map "/series=/srv/media/series"
```

For Radarr use `--arr radarr`, its URL/API key, and the relevant library map,
for example `/movies=/srv/media/movies`. Start the command as soon as the
chosen release appears in Downloadarr.

The command fails on authentication/API errors, early removal, mismatched file
records, missing mapped output, retained staging data, or timeout. If the
verifier must restart after import began, use a bounded `--lookback` value.

Without path maps it still proves Arr import history, the library database
record, and removal from Downloadarr. With maps it additionally verifies host
files.

## Deterministic tests

The ordinary suite uses fake Arr and Downloadarr clients to exercise the same
state machine without network calls or external mutations:

```bash
PYTHONPATH=src python -m pytest -q
```
