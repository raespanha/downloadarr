# Dashboard

Downloadarr serves its authenticated dashboard at `http://HOST:6500/`. Sign in
with the same qBittorrent-compatible username and password configured in
Sonarr/Radarr.

The first dashboard slice provides:

- live job state, category, local byte progress, speed, ETA, and errors;
- explicit TorBox queue/provider/local-delivery phase labels;
- removal controls that also clean up the corresponding TorBox object;
- masked TorBox credentials; and
- atomically saved download settings and category paths.

The page polls the local API every two seconds. Job names and provider errors
are inserted as text rather than HTML. State-changing forms reject cross-origin
browser requests and the session cookie is HTTP-only with strict same-site
handling.

Settings are validated and saved to the configured JSON file with the existing
timestamped-backup behavior. They take effect after Downloadarr restarts. An
empty token field preserves the stored token, and the current secret is never
rendered into the page.

Environment-managed fields are listed in the settings panel. A saved file
cannot override those fields while their environment variables remain set.
