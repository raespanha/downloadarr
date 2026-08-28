# Roadmap

Downloadarr is currently an alpha project. The implemented baseline includes
the qBittorrent-compatible Sonarr/Radarr facade, TorBox submission and polling,
durable resumable delivery, operational controls, monitoring, backups, and a
hardened single-process container deployment.

## Before a stable release

- Validate performance, backup, restore, and rollback on the supported native
  Linux deployment described in the production runbook.
- Expand end-to-end coverage across representative Sonarr and Radarr releases
  without adding external downloads to routine CI.
- Publish signed, immutable container images with vulnerability scanning, SBOM,
  and provenance.
- Document upgrades and compatibility guarantees between releases.

## Possible later work

- Additional debrid providers behind the existing provider interface.
- Optional archive handling with explicit resource and safety limits.
- More notification and observability integrations.
- Multi-process scheduling only after leases and concurrency semantics are
  designed and tested; the current supported architecture remains one process.

Roadmap items are intentions, not commitments. Discuss substantial API or
architecture changes in an issue before implementation.
