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
stress-test -> /downloads/stress-test
```

Never run this test automatically during application startup or routine CI.

## 2026-08-06 container run

The test was submitted through Downloadarr's live qBittorrent facade. Results:

- duplicate TorBox torrent reconciled by info hash;
- four ranged connections used by the downloader;
- API progress advanced from 0% to 100%;
- sustained displayed throughput settled near 18 MiB/s;
- final state was `stalledUP`;
- content path was `/downloads/stress-test/debian-13.6.0-amd64-netinst.iso`;
- exact byte count and Debian SHA-256 matched;
- no `.part` or manifest remained after publication;
- completed state survived a container restart; and
- `deleteFiles=true` removed only the Debian fixture and its job record.

An unrelated sibling directory was preserved, confirming scoped cleanup.

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

## Four versus eight connections

A second run used the same cached torrent, destination, host, and verification
procedure with the provider ceiling temporarily raised to eight connections.

| Connections | End-to-end time | Observed behavior |
|---:|---:|---|
| 4 | approximately 44 seconds | Stable near 18 MiB/s |
| 8 | 187.76 seconds | Initial recoverable stall; later bursts up to 12.7 MiB/s |

The eight-connection run had an effective end-to-end rate of roughly 4 MiB/s,
completed with the correct byte count and SHA-256, and left no temporary files.
This is a single-route sample rather than a statistically controlled network
benchmark, but it strongly supports retaining TorBox's four-connection ceiling.
The live setting was restored to four after cleanup.

## Future UI test

The future UI speed test should call the same delivery service and add explicit
confirmation, cancellation, 1-4 connection comparison, live throughput,
official hash verification, and optional automatic cleanup. Results should be
clearly separated from normal Sonarr/Radarr jobs.

## 2026-08-07 browser-class performance vertical

A fresh same-URL disk comparison measured native Windows curl at 42.67 MB/s
and Downloadarr `auto` mode at 40.69 MB/s. Both transferred exactly 791674880
bytes and matched the fixture SHA-256. Downloadarr reached 95.35% of the native
disk baseline.

Docker Desktop was independently limited to approximately 18.2 MB/s with both
raw aiohttp and Linux curl. Downloadarr measured 17.70 MB/s on container-local
storage and 17.48 MB/s through the Windows bind mount, ruling out the writer
and bind mount as the primary cause. Four dynamically scheduled ranges raised
the container result to 20.63 MB/s with no retries and a matching hash.

All temporary benchmark outputs were removed after verification. Signed URLs
and credentials were not stored.
