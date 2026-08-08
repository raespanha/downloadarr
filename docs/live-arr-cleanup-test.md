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

If the verifier itself must be restarted after the import has already begun or
completed, use `--lookback 900` (or another bounded number of seconds) so it may
accept that recent Arr history event while it waits for Downloadarr cleanup.

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

## 2026-08-08 Sonarr cycle

A complete S03E06 replacement cycle validated the command and the live stack:

- the existing 8.23 GB Sonarr library file was removed;
- two stale Downloadarr jobs, their TorBox torrent/queue items, and staging data
  were removed;
- Sonarr interactive search selected a different accepted cached torrent;
- Downloadarr delivered exactly `6338119104` bytes at approximately
  18.6 MiB/s without transfer retries;
- Sonarr imported and registered the exact-size replacement as file ID 150;
- Sonarr automatically called Downloadarr's completed-download removal path;
- the Downloadarr job, TorBox remote torrent, Sonarr queue entry, and staging
  directory were absent afterward; and
- the replacement library file remained at exactly `6338119104` bytes.

The cycle exposed and corrected three integration defects:

1. `as_queued=true` forced every TorBox submission into its manual queue;
2. missing zero seed limits prevented Sonarr from considering `pausedUP` debrid
   jobs eligible for removal; and
3. the live verifier used a lowercase history filter even though Sonarr's
   qBittorrent download IDs and filter are uppercase/case-sensitive.

## 2026-08-08 Radarr cycle

The equivalent destructive replacement cycle also passed for Radarr using
`The Lion King 1½ (2004)` as the smallest existing library target:

- Radarr movie file ID 8 and its exact `807310164`-byte physical file were
  removed while the monitored movie record remained intact;
- an accepted 1080p replacement with info hash
  `bb52d50d798d8b55994538cb73d9ae3cb22c943e` was grabbed through Radarr;
- TorBox exposed the cached `1328822240`-byte torrent as remote ID `72413019`;
- Downloadarr delivered both torrent files, reported `pausedUP`, and the live
  verifier observed Radarr's import-and-cleanup contract after 154.9 seconds;
- Radarr registered movie file ID 18 with the exact video size `1328716257`;
- the Radarr queue, Downloadarr job, TorBox torrent, TorBox queue record, and
  staging directory were all absent after cleanup; and
- the imported library file remained on the host at exactly `1328716257`
  bytes.

This completes live Sonarr and Radarr coverage of the same automated verifier.
