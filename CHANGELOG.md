# Changelog

## 0.1.0 (unreleased)

First public version.

- One `wristconf` command: `scan`, `use`, `pair`, `finish-setup`, `info`, `snapshot` (full and quick),
  `show`, `set`, `plan`, `apply`, `check`, `find`, `diff`, `catalog build|sanitize|render|list`.
- Settings files (TOML) with plan/apply. Values already set are skipped. A change that depends on an earlier
  one (rename after add) is checked again right before writing. `plan --offline` simulates against a snapshot.
- Sanitized catalogs of the fenix 8 47 mm, firmware 21.43 and 23.40, and a browsable map in `docs/maps/`.
- Safety rails: no factory reset, no OAuth replies, no Connect IQ screens, destructive items and confirmation
  dialogs blocked, values checked against the watch's limits, re-read after every write.
- Tested on Linux. macOS and Windows code paths are in place but untested with a watch.
