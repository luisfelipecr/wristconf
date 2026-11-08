# Contributing

Thanks for helping. Three things help most right now.

## 1. A catalog of your watch or firmware

Every model and firmware has a different settings tree. A sanitized catalog makes `find` and `check` work for
everyone with that watch.

```bash
wristconf snapshot --out my-snapshot          # read-only, ~30 min
wristconf catalog sanitize my-snapshot -o catalog.json
```

`sanitize` removes values, states, list orders, numeric limits, battery estimates and the serial number. Then it
prints the labels no bundled catalog has: **read that list**. Names you gave things (a renamed activity, a custom
data screen) show up there; rename them back in the JSON or tell us in the pull request.

Put the file at `src/wristconf/catalogs/<model>/fw-<version>.json` (`wristconf catalog list` shows the naming;
the model and firmware come from Settings > System > About), and regenerate the map:

```bash
wristconf catalog render src/wristconf/catalogs/<model>/fw-<version>.json --split -o docs/maps/<model>-fw-<version>
```

**Never attach a snapshot** (the folder) to an issue or pull request: it holds your personal values.

## 2. macOS and Windows reports

Pairing on macOS and Windows has not been tried with a watch yet. If you try, open an issue with your OS
version, watch model, what you ran and what happened, even if it failed. See [docs/PLATFORMS.md](docs/PLATFORMS.md).

## 3. Protocol findings

New facts about the protocol go in [docs/PROTOCOL.md](docs/PROTOCOL.md), labeled as verified (seen on a watch)
or hypothesis. The open problem is the menus without a target (Save, Remove from List).

## Code

```bash
uv sync
uv run pytest
uv run ruff check src tests
```

- Tests run without a watch: `tests/fakewatch.py` serves a small settings tree through the same interface as a
  real connection. A change to `change.py` comes with a test there.
- Safety rails (see [docs/SAFETY.md](docs/SAFETY.md)) live in `protocol.py` and `change.py`. A pull request that
  loosens one must say why and how it was tested on a real watch.
- `src/wristconf/_proto/` is generated: edit `proto/garmin/*.proto` and run `scripts/gen_proto.sh`.
- Commit messages: conventional prefix (`feat:`, `fix:`, `docs:`…), in English.

By contributing you agree that your contribution is licensed under the AGPL-3.0, like the rest of the project.
