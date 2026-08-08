# Automated Arr Import and Cleanup Test

This opt-in live test verifies the final Sonarr/Radarr integration boundary:

1. the target job exists in Downloadarr;
2. Arr records `downloadFolderImported` for the same torrent info hash;
3. Arr registers the imported episode or movie file with the expected size;
4. Arr removes the completed job through Downloadarr's qBittorrent facade; and
5. when path maps are supplied, the library file remains while the staging path
   has been removed.

The test is passive. It never searches for, grabs, or submits a release. This
keeps routine test runs and CI from downloading content or changing a media
library. Use a small, explicitly approved legal media fixture that the selected
Arr application can identify.

## Run it

Install the package, then provide secrets through process-scoped environment
variables. Do not put these values in Git, shell history, screenshots, or test
reports.

PowerShell example for Sonarr:

```powershell
$env:DOWNLOADARR_E2E_ARR_URL = "http://127.0.0.1:8989"
$env:DOWNLOADARR_E2E_ARR_API_KEY = "<sonarr-api-key>"
$env:DOWNLOADARR_E2E_DOWNLOADARR_URL = "http://127.0.0.1:6500"
$env:DOWNLOADARR_E2E_USERNAME = "<downloadarr-user>"
$env:DOWNLOADARR_E2E_PASSWORD = "<downloadarr-password>"

downloadarr-verify-arr-cleanup `
  --arr sonarr `
  --hash <40-character-torrent-info-hash> `
  --timeout 7200 `
  --path-map "/torbox=C:\torbox_media" `
  --path-map "/series=C:\plex_media\series"
```

For Radarr, use `--arr radarr`, port `7878`, the Radarr API key, and the
relevant staging/library maps, for example `/movies=C:\plex_media\movies`.

Start the command immediately after the chosen release appears in Downloadarr.
The command exits successfully only after the complete import-and-cleanup
contract has been observed. It exits nonzero on authentication errors, API
errors, early removal, mismatched file records, missing mapped output, retained
mapped staging data, or timeout.

Path maps are optional. Without them, the verifier still proves Arr's import
history, Arr's library database record, and removal from Downloadarr. With
them, it additionally checks the physical host files.

## Normal automated tests

The ordinary test suite uses fake Arr and Downloadarr clients to exercise the
same state machine without network access or external mutations:

```powershell
python -m pytest -q
```

These deterministic tests cover successful cleanup, cleanup before import,
missing jobs, and physical library/staging validation. The live command remains
opt-in because a genuine Arr import necessarily changes external state.
