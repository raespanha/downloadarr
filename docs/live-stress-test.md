# Live TorBox Stress Test

This is an opt-in external integration test, not part of the unit-test suite or
CI. It transfers approximately 792 MB and requires a real TorBox account.

## Fixture

The fixture is Debian's legal amd64 netinst image:

| Property | Value |
|---|---|
| Info hash | `481b6e3617be4c88f96cb25e47c9d8272130071e` |
| Filename | `debian-13.6.0-amd64-netinst.iso` |
| Size | `791674880` bytes |
| SHA-256 | `65273beed27b2df543b68b65630ba525cfbad8df2b12035732b2dff87d6664e7` |

Use a dedicated qBittorrent category such as:

```text
stress-test -> /torbox/rd-test
```

Never run this test automatically during application startup or routine CI.

## 2026-08-06 container run

The test was submitted through Downloadarr's live qBittorrent facade running
as the `rdt-client` Compose service. Results:

- duplicate TorBox torrent reconciled by info hash;
- four ranged connections used by the downloader;
- API progress advanced from 0% to 100%;
- sustained displayed throughput settled near 18 MiB/s;
- final state was `stalledUP`;
- content path was `/torbox/rd-test/debian-13.6.0-amd64-netinst.iso`;
- exact byte count and Debian SHA-256 matched;
- no `.part` or manifest remained after publication;
- completed state survived a container restart; and
- `deleteFiles=true` removed only the Debian fixture and its job record.

An unrelated Ubuntu directory already present under `/torbox/rd-test` was
preserved, confirming scoped cleanup.

## Defects found by the run

The exercise exposed and fixed three integration defects:

1. The container SQLite URL used a relative path and bypassed `/config`.
   Container settings now use `sqlite+aiosqlite:////config/downloadarr.db`.
2. A stale TorBox queue record could hide an already-ready torrent with the
   same hash. Reconciliation now prefers the active torrent when available.
3. TorBox returns HTTP 400 for a magnet already attached to the account.
   Rejected duplicate submissions now reconcile against active and queued
   items by validated info hash instead of failing immediately.

Regression tests cover each corrected behavior where applicable.

## Future UI test

The future UI speed test should call the same delivery service and add explicit
confirmation, cancellation, 1-4 connection comparison, live throughput,
official hash verification, and optional automatic cleanup. Results should be
clearly separated from normal Sonarr/Radarr jobs.
