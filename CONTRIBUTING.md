# Contributing

Thanks for considering a contribution to Downloadarr.

Before opening a pull request:

1. Discuss substantial behavior or API changes in an issue first.
2. Keep credentials, signed URLs, magnets, filenames, local paths, databases,
   media, and logs out of commits and issues.
3. Add focused tests and run `PYTHONPATH=src python -m pytest -q`.
4. Run `python -m compileall -q src` and `git diff --check`.
5. Keep changes compatible with a single-process SQLite deployment unless the
   proposal explicitly changes and tests that architecture.

Use only legal, replaceable fixtures for opt-in integration tests. Never make
external downloads part of the normal test suite.

Report suspected vulnerabilities privately as described in
[SECURITY.md](SECURITY.md), not through a public issue.
