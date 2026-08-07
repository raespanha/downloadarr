# Warning — Before Making the Repository Public

> **Warning:** Do not make this repository public until every item below has
> been checked. Local diagnostic logs and development conversations may have
> exposed service credentials even when those credentials were never committed
> to Git.

## Required security checks

- Rotate the Prowlarr API key. Sonarr history and debug logs can embed it in
  download URLs.
- Rotate the TorBox API token used during development and update the private
  `config/settings.json` file afterward.
- Rotate the qBittorrent-compatible username, password, and API key if they
  were copied into logs, screenshots, commands, or conversations.
- Confirm that `config/settings.json`, SQLite databases, backups, partial
  downloads, manifests, media files, and application logs remain ignored.
- Search the complete Git history—not only the current working tree—for API
  keys, tokens, passwords, signed URLs, magnets containing private parameters,
  and copied configuration files.
- Review screenshots, fixtures, documentation, issue text, benchmark output,
  and captured HTTP logs for credentials and personally identifying paths.
- Remove or sanitize local files such as `rdt-client-capture.log` before
  attaching or publishing them anywhere.
- Run a secret scanner against the full repository history and resolve every
  finding before changing repository visibility.
- Verify from a clean clone that the application starts only after the user
  supplies their own settings and credentials.

Rotating a credential is required after exposure. Deleting or masking it in a
later commit does not invalidate copies preserved in Git history, logs, tool
output, screenshots, or conversations.
