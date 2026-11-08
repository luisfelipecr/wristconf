"""Settings as code: a TOML file lists the settings you want; wristconf shows the plan, then applies it.

    name = "Minimal run screens"
    description = "..."
    model = "fenix 8"          # informational

    [[change]]
    path = "Settings > Activities & Apps > Activities > Run"
    item = "Accent Color"
    choice = "Blue"

    [[change]]
    path = "Settings > Activities & Apps > Activities > Run"
    item = "Auto Pause"
    switch = "off"

Keys of a change: path, item and exactly one of choice, switch, int, text, time ("HH:MM"),
duration (seconds), float, select, move_to, remove; optionally entry_id and check ("PATH|ITEM|VALUE").
Changes run in file order, in one connection, and stop at the first failure.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from wristconf.change import Change, ChangeError


@dataclass
class Preset:
    name: str
    description: str = ""
    model: str | None = None
    changes: list[Change] = field(default_factory=list)


def load(path: Path) -> Preset:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ChangeError(f"{path}: invalid TOML: {exc}") from None
    extra = set(data) - {"name", "description", "model", "change"}
    if extra:
        raise ChangeError(f"{path}: unknown top-level keys {sorted(extra)}")
    raw = data.get("change", [])
    if not isinstance(raw, list) or not raw:
        raise ChangeError(f"{path}: no [[change]] entries")
    changes = []
    for i, d in enumerate(raw, 1):
        try:
            changes.append(Change.from_dict(d))
        except (ChangeError, TypeError) as exc:
            raise ChangeError(f"{path}: change #{i}: {exc}") from None
    return Preset(name=data.get("name", Path(path).stem), description=data.get("description", ""),
                  model=data.get("model"), changes=changes)


_KIND_RULES = {  # kind -> (allowed rows, allowed targets); None = any
    "choice": (None, {"TYPE_LIST"}),
    "switch": ({"ROW_SWITCH"}, None),
    "int": (None, {"TYPE_INTEGER"}),
    "text": ({"ROW_TEXT"}, None),
    "time": (None, {"TYPE_TIME_OF_DAY"}),
    "duration": (None, {"TYPE_DURATION"}),
    "float": (None, {"TYPE_FLOAT"}),
    "select": ({"ROW_ACTION", "ROW_SINGLE_ACTION"}, None),
}


def check(preset: Preset, catalog: dict) -> list[str]:
    """Problems found by checking a preset against a catalog, without a watch.

    Catches typos in paths, items and choices. A change that comes after one that adds, renames or
    reorders things is not checked (its screen may not exist yet). Availability (an item the watch
    shows as unavailable for that activity) is only known live.
    """
    from wristconf.change import split_path

    by_path: dict[str, list[dict]] = {}
    for sc in catalog["screens"].values():
        by_path.setdefault(" > ".join(split_path(sc["path"])), []).append(sc)
    problems, structural = [], False
    for n, c in enumerate(preset.changes, 1):
        kind = c.kind()
        where = f"change #{n} ({c.path} › {c.item})"
        if structural:
            continue
        structural = kind in ("select", "text", "move_to", "remove")
        screens = by_path.get(" > ".join(split_path(c.path)))
        if not screens:
            problems.append(f"{where}: no screen with this path in the catalog")
            continue
        if kind in ("move_to", "remove"):
            continue  # sortable list entries are personal and not kept in sanitized catalogs
        items = [e for sc in screens for e in sc["entries"] if e["label"] == c.item]
        if c.entry_id is not None:
            items = [e for e in items if e["id"] == c.entry_id]
        rows, targets = _KIND_RULES.get(kind, (None, None))
        typed = [e for e in items if (rows is None or e["row"] in rows) and (targets is None or e["target"] in targets)]
        if not items:
            names = sorted({e["label"] for sc in screens for e in sc["entries"] if e["label"]})
            problems.append(f"{where}: no item with this name. Items: {names}")
        elif not typed:
            problems.append(f"{where}: the item is {items[0]['row']}/{items[0]['target']}, which does not take {kind}")
        elif kind == "choice":
            ok = [e for e in typed if c.choice in [x["label"] for x in e.get("options", {}).get("choices", [])]]
            if not ok:
                choices = [x["label"] for x in typed[0].get("options", {}).get("choices", [])]
                problems.append(f"{where}: '{c.choice}' is not a choice. Choices: {choices}")
    return problems
