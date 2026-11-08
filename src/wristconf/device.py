"""Finding the watch, per-user folders and the saved default device."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_UUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def looks_like_address(s: str) -> bool:
    """A MAC (Linux, Windows) or a CoreBluetooth UUID (macOS hides MAC addresses)."""
    return bool(_MAC.match(s) or _UUID.match(s))


def _base_dir(kind: str) -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "wristconf"
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") if kind == "config" else os.environ.get("LOCALAPPDATA")
        return Path(root or home) / "wristconf"
    if kind == "config":
        return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "wristconf"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "wristconf"


def config_dir() -> Path:
    return Path(os.environ["WRISTCONF_CONFIG_DIR"]) if os.environ.get("WRISTCONF_CONFIG_DIR") else _base_dir("config")


def data_dir() -> Path:
    """Snapshots and change logs. They hold YOUR settings (weight, birth date, zones): keep them private."""
    return Path(os.environ["WRISTCONF_DATA_DIR"]) if os.environ.get("WRISTCONF_DATA_DIR") else _base_dir("data")


def _config_file() -> Path:
    return config_dir() / "config.json"


def saved_device() -> str | None:
    try:
        return json.loads(_config_file().read_text()).get("device")
    except (OSError, ValueError):
        return None


def save_device(device: str) -> Path:
    f = _config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(f.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg["device"] = device
    f.write_text(json.dumps(cfg, indent=2) + "\n")
    return f


def resolve_device(cli_value: str | None) -> str | None:
    """--device, then $WRISTCONF_DEVICE, then the saved default. None = scan for a Garmin-looking name."""
    return cli_value or os.environ.get("WRISTCONF_DEVICE") or saved_device()


async def scan(timeout: float = 12.0) -> list[dict]:
    from bleak import BleakScanner
    from garmin_ble.transport.ble import looks_like_garmin

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        rows.append({"address": addr, "name": name, "rssi": adv.rssi, "garmin": bool(looks_like_garmin(name))})
    rows.sort(key=lambda r: (not r["garmin"], -(r["rssi"] or -999)))
    return rows
