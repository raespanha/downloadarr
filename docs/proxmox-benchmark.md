# Proxmox performance benchmark

This benchmark distinguishes container overhead from CDN variance and storage bottlenecks on
the supported native Linux/Proxmox deployment.

## Method

1. Use a deterministic local HTTP server implementing byte ranges. Test host and container with
   the same bytes/route, first on local ext4/ZFS and then on the intended media mount.
2. Run `1,4,8,16` connections, at least five repetitions each, in randomized order. Verify every
   SHA-256. Capture median/p95 throughput, retries, CPU, RSS, network, disk latency/utilization,
   filesystem, Docker network mode, and image digest.
3. Repeat with one legal TorBox object in the same time window. This is indicative because CDN
   routing and signed-link timing are confounders. Never persist the signed URL/token.
4. If media is slow, compare download-to-local plus a separate move to isolate network vs disk.

```console
downloadarr-benchmark "$SIGNED_URL" \
  --connections 1,4,8,16 --repetitions 5 \
  --sha256 "$EXPECTED_SHA256" --output-root /benchmark
```

The opt-in runner uses one scoped temporary directory, deletes only its own files, redacts the
URL, and emits JSON with hashes and samples. Check free space and bandwidth budget first.

Initial acceptance: container median on local storage is within 15% of host median at the same
connection count, with exact hashes and no protocol errors. This is diagnostic, not a TorBox
speed promise.
