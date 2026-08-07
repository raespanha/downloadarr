# Tomorrow Task: Restore Browser-Class Delivery Speed

Planned for: 2026-08-07  
Status: Implemented and verified
Target: One focused performance vertical

## Implementation result

Completed on 2026-08-07 with:

- accurate current-session byte and speed accounting for resumed transfers;
- credential-safe CDN hostname, range, and retry diagnostics;
- a CLI URL-from-environment option and JSON diagnostics mode;
- browser-style full `200` GETs for fresh `auto` transfers;
- explicit `sequential` and `parallel` transfer modes;
- dynamic parallel segmentation with eight segments per worker;
- capability-selected Linux `os.pwrite` and independent-handle fallback
  positional writes;
- structured background checkpoints that snapshot durable progress without
  holding the network write path during manifest publication; and
- 62 passing tests on host Python 3.13 and container Python 3.12.

The same fresh legal fixture URL and destination produced:

| Environment and client | Mode | Result |
|---|---|---:|
| Windows native curl to disk | Full GET | 42.67 MB/s |
| Windows Downloadarr | `auto` full GET | 40.69 MB/s |
| Docker Desktop raw aiohttp | Full GET to null sink | 18.17 MB/s |
| Docker Desktop Linux curl | Full GET to null sink | 18.16 MB/s |
| Docker Desktop Downloadarr, internal `/tmp` | `auto` full GET | 17.70 MB/s |
| Docker Desktop Downloadarr, Windows bind mount | `auto` full GET | 17.48 MB/s |
| Docker Desktop Downloadarr, internal `/tmp` | 4-worker `parallel` | 20.63 MB/s |

Every complete Downloadarr result matched the expected size and SHA-256. The
Windows native result reached 95.35% of the native disk baseline and exceeded
the 90% acceptance threshold. Container host networking did not improve the
bounded sample. Matching raw aiohttp and Linux curl results, plus equivalent
`/tmp` and bind-mount results, isolate the remaining container gap to Docker
Desktop's Linux network path rather than Downloadarr or its writer.

The local Windows Docker deployment therefore explicitly uses `parallel`
with the four-connection TorBox ceiling. `auto` remains the portable default
and selects the measured browser-style full GET for fresh downloads.

## Objective

Make Downloadarr approach the throughput of a browser downloading the same
TorBox object, without weakening byte validation, resumability, cancellation,
or atomic publication.

This task was successful when a fresh Downloadarr transfer reached at least
90% of the native single-stream baseline for the same signed URL and produces
the expected SHA-256 digest.

## Verified baseline

The controlled legal fixture is Debian's amd64 netinst image documented in
`live-stress-test.md`:

| Property | Value |
|---|---:|
| Size | `791674880` bytes |
| Expected SHA-256 | `65273beed27b2df543b68b65630ba525cfbad8df2b12035732b2dff87d6664e7` |

Measurements from the same fresh TorBox CDN URL on the Windows host:

| Client | Mode | Result |
|---|---|---:|
| Native curl | One full HTTP/1.1 GET to the null device | 43.00 MB/s |
| Downloadarr | 16 fixed ranges, Windows host filesystem | 31.26 MB/s |

The Downloadarr result used ranges, did not resume, transferred the exact byte
count, and matched the expected SHA-256. The temporary output was removed.

A different cached file was served from another node in the same South Europe
CDN region and measured much more slowly:

| Client | Result |
|---|---:|
| Native curl, one full GET | 5.76 MB/s |
| Downloadarr, 16 ranges | approximately 8.9 MB/s |

This proves two separate effects:

1. TorBox object/node routing can dominate achievable throughput.
2. On a fast route, Downloadarr currently leaves approximately 27% of the
   available single-stream throughput unused.

Do not store signed CDN URLs, TorBox tokens, or private media names in tests,
logs, fixtures, documentation, or commits.

## Corrected diagnosis

The browser and API were not generating different links for the Debian
fixture. TorBox's API reproduced the exact CDN host and object path supplied by
the dashboard. Earlier comparisons accidentally used different files hosted
by different CDN nodes.

HTTP/2 or HTTP/3 is also not required to explain the fast result: the native
43 MB/s control completed over HTTP/1.1.

The current downloader has several likely local bottlenecks:

- every 256 KiB block waits for a serialized seek and write;
- all chunks share one file-position lock;
- durable data and manifest checkpoints execute on the active write path;
- equal-sized, long-lived ranges make total completion wait for the slowest
  connection; and
- a one-connection ranged transfer cannot currently select a normal full
  `200` response for servers where full GET is faster than `206` ranges.

The displayed average for resumed downloads is also incorrect because it
divides the whole file size by only the current session's elapsed time. Fix it
as part of the instrumentation work so future comparisons are trustworthy.

## Implementation sequence

### 1. Add repeatable benchmark instrumentation

- Add an opt-in benchmark command or script that accepts a URL through an
  environment variable or callback without printing it.
- Record time to first byte, session bytes, wall time, average throughput,
  response mode, range count, retry count, and per-range completion times.
- Record only the CDN hostname, never its signed path or query.
- Keep the live benchmark outside routine unit tests and CI.
- Verify size and SHA-256 and clean up only its explicitly scoped fixture.

### 2. Correct transfer accounting

- Track bytes transferred during the current session separately from restored
  manifest bytes.
- Calculate session speed from session bytes divided by session elapsed time.
- Preserve total downloaded bytes for overall progress reporting.
- Add regression coverage for resumed speed and progress calculations.

### 3. Remove the shared seek/write bottleneck

- Introduce a positional-writer interface with the existing implementation as
  a compatibility fallback.
- Use `os.pwrite` through bounded worker threads on Linux so writes do not
  share or mutate a file cursor.
- Select the implementation by capability, not by hard-coded platform name.
- Keep exact short-write and disk-error checks.
- Benchmark both the Windows host and the Python 3.12 Linux container.

### 4. Move checkpoints off the hot path

- Let successful writes update in-memory progress without waiting for an
  immediate manifest transaction.
- Use one structured background checkpoint task with byte and time triggers.
- Preserve the ordering rule: file data must be flushed before the manifest
  claims those bytes are durable.
- On cancellation or failure, stop new writes, flush completed data, persist
  the final resumable state, and await the checkpoint task.

### 5. Replace fixed ranges with dynamic segments

- Divide ranged downloads into substantially more segments than workers.
- Feed segments through a bounded queue so a fast worker can claim more work
  instead of waiting for one slow, large range.
- Preserve per-segment progress in the manifest and allow continuation from a
  partial segment.
- Keep concurrency at or below the configured provider ceiling.
- Ensure one terminal segment failure cancels and awaits sibling workers.

### 6. Add an adaptive transfer strategy

- Support a normal full `200` GET for a fresh one-stream transfer.
- Preserve ranged continuation for resumes where the server validates ranges.
- Compare full-GET and ranged modes using controlled measurements rather than
  assuming that more connections are always faster.
- Do not race duplicate full-file transfers. Any sampling must be small,
  bounded, and included in bandwidth accounting.
- Retain the conservative TorBox default while allowing an explicit opt-in
  stress comparison up to 16 connections.

### 7. Expose routing diagnostics safely

- Include the CDN hostname and chosen transfer mode in debug/status output.
- Never expose the signed path, query, token, or authorization header.
- Document TorBox's optional `user_ip` request parameter, but do not add an
  external public-IP lookup or transmit a user address without explicit user
  configuration and consent.
- Treat CDN selection as an upstream/account setting; do not silently change
  the user's TorBox account configuration.

## Test plan

### Deterministic automated tests

- sequential `200` full GET;
- strict ranged `206` mode;
- dynamic work redistribution when one segment is slow;
- partial-segment retry and resume;
- cancellation during a background checkpoint;
- short positional write and disk-full failure;
- manifest durability ordering;
- correct fresh and resumed session speed;
- redaction of signed paths and query strings; and
- identical SHA-256 output with 1, 4, 8, and 16 configured workers.

### Opt-in live matrix

Run each case against the same newly generated Debian fixture URL, without
overlapping clients or reusing it concurrently:

| Environment | Modes |
|---|---|
| Native control | Full GET to null device and to disk |
| Windows host | Downloadarr full GET, 1 range, 4 ranges, 16 ranges |
| Python 3.12 container | Downloadarr full GET, 1 range, 4 ranges, 16 ranges |

For every disk result, require the expected byte count and SHA-256. Report
median throughput from at least three runs when bandwidth cost and TorBox fair
use permit it; otherwise label single samples clearly.

## Acceptance criteria

- The optimized downloader reaches at least 90% of the native disk-writing
  baseline on the same fast URL, measured with verified fresh transfers.
- No benchmark relies on the known inflated resume-speed calculation.
- Dynamic scheduling does not exceed the configured connection ceiling.
- Failed and cancelled transfers remain resumable and never publish a final
  file.
- Successful transfers publish atomically and match the expected SHA-256.
- Unit and integration suites pass on host Python 3.13 and container Python
  3.12.
- Live tests remain explicit, credential-safe, and disabled in normal CI.

## Out of scope

- Changing the user's TorBox CDN/account selection automatically.
- Promising a fixed speed when TorBox assigns a slow object or CDN node.
- UI speed-test implementation; this task should first establish a reliable
  downloader benchmark API that the future UI can call.
- Sonarr/Radarr path mapping or import behavior unrelated to delivery speed.

## Follow-up starting point

Build the future UI benchmark on the credential-safe result fields. If Windows
Docker Desktop must match host-native speed, evaluate a host-side delivery
worker or future Docker Desktop/WSL networking changes separately; replacing
aiohttp with Linux curl does not address the measured environment ceiling.
