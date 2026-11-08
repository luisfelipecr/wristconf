# Safety

wristconf writes to a device you wear and depend on, through a protocol Garmin does not document. These rails
are in the code, below the command line, so no command can skip them.

## What wristconf never does

- **Never sends a factory reset.** System events go through a fixed allow list (time updated, sync ready, pair
  complete, sync complete, setup complete). FACTORY_RESET is not on it, and the check is an explicit error, not
  an `assert` that `python -O` would remove.
- **Never answers OAuth** requests from the watch and never sends `SetDeviceSettings`. In Gadgetbridge these are
  the paths that may require a factory reset.
- **Never opens Connect IQ screens.** A fenix 8 rebooted with one in flight.
- **Never writes without being asked.** `set` and `plan` simulate. Writing needs `--apply` (or the `apply`
  command) **and** a yes at the prompt, or `--yes` typed in advance. Without a terminal and without `--yes`, it
  refuses.

## What it refuses to change

- Items with destructive names (Restore, Reset, Delete, Remove, Clear, Erase, Factory, Default), except removing
  an entry from a sortable list, which the watch marks as removable or not.
- Actions the watch asks to confirm (it sends a confirmation dialog in the item's definition).
- Action rows outside selection screens ("… > Add", "Options", "Choose Screen", "Edit Glances"…).
- Values outside the limits the watch sends for that item, and choices that are not in its list.
- Items the watch marks as unavailable in that context (`state = 1`).

## What every write does

1. Navigates the full path by item name (ids change between firmware versions, and some screens depend on the
   last screen opened).
2. Reads the item live and shows current → new. If the watch already has the value, nothing is sent.
3. Sends **one** change, reports any alert the watch shows (for example "Running Threshold Auto Detection
   Disabled"), re-reads and compares. A settings file stops at the first change that does not verify.

## Before you change things

- Take a snapshot (`wristconf snapshot`): it is your record of how things were. There is no automatic restore
  yet; the snapshot and `wristconf diff` tell you what to set back.
- After a firmware update, take a new full snapshot before writing: the tree changes.

## Your data

Snapshots and logs hold your own values: weight, birth date, heart rate zones, FTP, the serial number. They stay
in your data folder (`~/.local/share/wristconf` on Linux). Share only catalogs passed through
`wristconf catalog sanitize`, and read the list of labels it prints first.

This project is not affiliated with or endorsed by Garmin. Use it at your own risk (see the warranty
disclaimer in the LICENSE).
