# wristconf

Read, back up and change the settings of a Garmin watch **from your computer, over Bluetooth**: activity
profiles, data screens, glances, controls, display, focus modes, alerts. No Garmin Connect app, no cloud
account in the loop.

```console
$ wristconf find "accent color"
Settings > Activities & Apps > Activities > Run › Accent Color  [double_line / list]
    choices: Blue · Light Blue · Green · Light Green · Yellow · Orange · Red · Pink · …

$ wristconf plan examples/run-basics.toml
plan: 2 to change, 1 already set
  Settings > Activities & Apps > Activities > Run › Satellites: Auto Select → All + Multi-Band
  Settings > Activities & Apps > Activities > Run > Laps › Auto Lap: Auto Select → Off

Simulation only. Add --apply to write these changes.
```

> **Status: alpha.** Tested on one model, the **fenix 8 (47 mm)**, firmware 21.43 and 23.40, from **Linux**.
> macOS and Windows should work but have not been tried with a watch yet ([platforms](docs/PLATFORMS.md)).
> Not affiliated with or endorsed by Garmin.

## Why

- **Settings as code.** Describe what you want in a TOML file, preview it with `plan`, write it with `apply`.
  Re-running it is safe: what is already set is skipped. Share setups with friends, keep one per season.
- **A map of every setting.** The fenix 8 has 600+ settings screens. Browse them in
  [docs/maps](docs/maps/fenix-8-47mm-fw-23.40/README.md) or search them offline with `wristconf find`.
- **Snapshots and diffs.** Save the whole configuration of your watch, and see exactly what a firmware update or
  a sync changed.
- **Keyboard instead of buttons.** Set up a new activity with ten data fields in a minute, not ten.

## Install

Python 3.11 or newer.

```bash
pipx install git+https://github.com/luisfelipecr/wristconf
```

On Linux, wristconf uses BlueZ; `bluetoothctl` must be installed for pairing.

## First connection

The watch talks to one "phone" at a time. wristconf pairs your computer as a second phone; while you use it,
**turn Bluetooth off on your phone in its Settings app** (on an iPhone, the Control Center toggle is not enough).

```bash
wristconf scan                          # find the watch's address
wristconf use AA:BB:CC:DD:EE:FF         # save it as the default
wristconf pair                          # watch: Settings > Connectivity > Phone > Pair Phone
wristconf finish-setup                  # once: otherwise the watch drops the link after ~20 s
wristconf info                          # model and firmware: you are connected
```

macOS and Windows: see [docs/PLATFORMS.md](docs/PLATFORMS.md).

## Use

```bash
wristconf snapshot                      # read everything, ~30 min on a fenix 8 (resumes if the link drops)
wristconf snapshot --base <snapshot>    # same firmware: re-read values only, ~9 min, and print the diff
wristconf show "Settings > Activities & Apps > Activities > Run > Data Screens" --depth 1
wristconf set "Settings > Activities & Apps > Activities > Run" "Accent Color" --choice Blue
wristconf set "Settings > Activities & Apps > Activities > Run" "Accent Color" --choice Blue --apply
```

A quick snapshot reuses the definitions of its base, so numeric limits and newly added menus can be out of date.
Take a full one after a firmware update or after changing units.

`set` simulates unless you add `--apply`, and asks before writing. Paths use the **item names** you see on
the watch, joined with ` > `.

### Settings files

```toml
name = "Run basics"

[[change]]
path = "Settings > Activities & Apps > Activities > Run"
item = "Accent Color"
choice = "Blue"

[[change]]
path = "Settings > Activities & Apps > Activities > Run > Laps"
item = "Auto Lap"
choice = "Off"
```

```bash
wristconf check my-setup.toml           # typos in paths, items and choices, against the map (no watch)
wristconf plan my-setup.toml            # what would change on your watch
wristconf plan my-setup.toml --offline <snapshot>   # the same, against a snapshot (no watch)
wristconf apply my-setup.toml           # write it, after a yes
```

A change takes one of `choice`, `switch` (on/off), `int`, `text`, `time` ("HH:MM"), `duration` (seconds),
`float`, `select = true` (pick an action row, e.g. add an activity), `move_to` or `remove = true` (sortable
lists). More in [examples/](examples/).

### All commands

| command | needs the watch | what it does |
|---|---|---|
| `scan`, `use`, `pair`, `finish-setup` | yes | find, save and pair the watch |
| `info` | yes | model and firmware versions |
| `snapshot` | yes | read every settings screen into a local folder (read-only) |
| `show PATH` | yes | read one screen live (read-only) |
| `set PATH ITEM --<value>` | yes | change one setting (simulates unless `--apply`) |
| `plan FILE` / `apply FILE` | yes (or `--offline`) | preview / write a settings file |
| `check FILE` | no | check a settings file against the map |
| `find TEXT` | no | search the map |
| `diff OLD NEW` | no | compare two snapshots or catalogs by path |
| `catalog build / sanitize / render / list` | no | work with settings maps |

## Safety

wristconf refuses factory resets, never answers OAuth, never opens Connect IQ screens, blocks destructive items
and actions the watch asks to confirm, checks every value against the limits the watch sends, and re-reads after
each write. Details in [docs/SAFETY.md](docs/SAFETY.md). It is still unofficial software writing to your watch:
take a snapshot first.

**Your data stays local.** Snapshots hold your weight, birth date, zones and serial number, in your data folder
(`~/.local/share/wristconf` on Linux). The catalogs in this repository are **sanitized**: structure only.

## How it works

Garmin watches expose their settings menus over Bluetooth through a protobuf service ("Realtime settings"), the
same one the Garmin Connect app uses. [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) reverse
engineered it for Android; wristconf uses Gadgetbridge's protobuf definitions on top of the GFDI transport of
[garmin-ble](https://github.com/gwerneckp/garmin-ble), and adds navigation by name, simulation, verification,
snapshots and settings files. What was learned on a real watch is in [docs/PROTOCOL.md](docs/PROTOCOL.md).

Not done yet: saving a **new** custom data screen, copying an activity and "Remove from List" (the protocol for
those menus is not known yet, see PROTOCOL.md); restoring a snapshot automatically.

## Contributing

The most useful contribution is a **sanitized catalog of another watch or firmware**, and reports from macOS
and Windows. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits and license

[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) (protocol research and protobuf definitions) and
[garmin-ble](https://github.com/gwerneckp/garmin-ble) (BLE transport and GFDI), both AGPL-3.0. wristconf is
licensed under the [GNU AGPL v3](LICENSE). Built by Luis Felipe Rodrigues with AI coding agents, and tested on
his own watch.

Garmin, fenix and Connect IQ are trademarks of Garmin Ltd. or its subsidiaries, used here only to say what this
software works with.
