# Dashboard

Downloadarr serves its authenticated dashboard at `http://HOST:6500/`. Sign in
with the same qBittorrent-compatible username and password configured in
Sonarr/Radarr.

The first dashboard slice provides:

- live job state, category, local byte progress, speed, ETA, and errors;
- durable 7-day, 30-day, and all-time performance timelines with weighted
  average speed, smoothed peak speed, delivered bytes, retries, and recent
  transfer details;
- service and indexer filters, comparison summaries, and persistent failure
  events with open/recovered state;
- explicit TorBox queue/provider/local-delivery phase labels;
- removal controls that also clean up the corresponding TorBox object;
- masked TorBox and Arr credentials; and
- atomically saved download settings and category paths.

The page polls the local API every two seconds. Job names and provider errors
are inserted as text rather than HTML. State-changing forms reject cross-origin
browser requests and the session cookie is HTTP-only with strict same-site
handling.

Settings are validated and saved to the configured JSON file with the existing
timestamped-backup behavior. Dashboard settings take effect immediately for
new work without interrupting an active HTTP transfer. **Simultaneous
downloads** controls the continuously replenished scheduler worker limit; each
file may still use multiple ranged connections up to the separate provider
ceiling. An empty token or API-key field preserves the stored secret, and
current secrets are never rendered into the page. Optional Sonarr/Radarr URLs
and API keys add exact indexer attribution through read-only grab-history
lookups.

The settings panel also exposes a video-extension allowlist and an optional
extra denylist. Executable/script suffixes remain permanently blocked in code,
and rejected files never reach the signed-URL or HTTP download stages.

When a destination file already exists, Downloadarr does not trust its name or
size alone. It compares distributed byte samples with authenticated ranges from
the provider. A matching file is reused and shown as verified; a mismatch
remains a visible failure so existing media is never silently overwritten.

Environment-managed fields are listed in the settings panel. A saved file
cannot override those fields while their environment variables remain set.

Performance data is stored independently from operational jobs, so normal Arr
post-import cleanup does not erase it. See `docs/performance-history.md` for
metric definitions, API details, logging, and retention behavior.
