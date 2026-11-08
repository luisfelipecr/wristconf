# AGENTS.md

Guide for AI coding agents (and humans) working on this repository.

## What this is

A Python CLI that reads and changes the settings of Garmin watches over Bluetooth LE, through the
"Realtime settings" protobuf service. Start with `README.md`, then `docs/PROTOCOL.md` (how the watch behaves,
and the traps that cost time) and `docs/SAFETY.md`.

## Map

- `src/wristconf/protocol.py`: connection, handshake, chunk reassembly, response matching, the write guard
  (`request_key`) and the system-event allow list. `Session` = one connection per process.
- `src/wristconf/snapshot.py`: full and quick snapshots (read-only), and `SnapshotSession` (offline replay).
- `src/wristconf/catalog.py`: build, sanitize, search and render catalogs. Paths use item names.
- `src/wristconf/change.py`: navigation by path, validation, simulation, write and verification.
- `src/wristconf/plan.py`: TOML settings files and the offline check.
- `src/wristconf/cli.py`: the command line. `device.py`: addresses and folders. `pairing.py`: per-OS pairing.
- `src/wristconf/catalogs/`: sanitized catalogs shipped with the package. `docs/maps/`: generated from them.
- `tests/fakewatch.py`: an in-memory watch with the same interface as `Session`.

## Rules

- **Never write to a real watch without the owner's explicit yes for that change** (show screen, item, current
  and new value first). Reading is fine.
- Do not loosen a safety rail (factory reset, OAuth, Connect IQ, destructive names, confirmation dialogs,
  limits) without saying so in the pull request and testing on a real watch.
- Never commit snapshots, logs, `.gble` captures or btmon logs: they hold personal data and serial numbers.
  Catalogs go through `wristconf catalog sanitize` and a read of its review list.
- Label protocol claims as verified or hypothesis in `docs/PROTOCOL.md`.
- `uv run pytest` and `uv run ruff check src tests` before declaring a change done.
