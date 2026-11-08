"""Pairing the computer with the watch as a new "phone".

Linux is the tested path (BlueZ, numeric comparison). macOS and Windows are written from how their
Bluetooth stacks behave and still need a test on real hardware: see docs/PLATFORMS.md.
"""

from __future__ import annotations

import sys
import time

WATCH_STEPS = (
    "On the watch: Settings > Connectivity > Phone > Pair Phone. Leave the QR code on screen.\n"
    "The watch accepts one phone link at a time: turn Bluetooth OFF on your phone in its Settings app\n"
    "(on an iPhone, the Control Center toggle is not enough)."
)


def pair_linux(address: str, timeout: float = 120.0, print_=print) -> bool:
    """Drives bluetoothctl: accepts the passkey on the computer side; you confirm it on the watch.

    Answering must be fast: waiting more than a few seconds makes the watch cancel the pairing.
    """
    import pexpect

    c = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=5)

    def send(x: str) -> None:
        c.sendline(x)
        time.sleep(0.5)

    send("agent off")
    send("agent KeyboardDisplay")
    send("default-agent")
    send("scan le")
    time.sleep(4)
    send(f"pair {address}")
    ok, answered = False, False
    deadline = time.time() + timeout
    while time.time() < deadline:
        i = c.expect([r"Confirm passkey (\d+) \(yes/no\):", r"Pairing successful", r"Failed to pair[^\r\n]*",
                      pexpect.TIMEOUT])
        if i == 0 and not answered:
            print_(f"Passkey {c.match.group(1)}: confirm the same number on the watch now.")
            c.sendline("yes")
            answered = True
        elif i == 1:
            send(f"trust {address}")
            ok = True
            break
        elif i == 2:
            print_(f"Pairing failed: {c.after}")
            break
    send("scan off")
    # bluetoothctl leaves the link up after pairing; the watch must be free for the next connection.
    send(f"disconnect {address}")
    send("quit")
    return ok


async def pair_windows(address: str, print_=print) -> bool:
    """bleak's WinRT backend can pair. Experimental: not yet tested with a Garmin watch."""
    from bleak import BleakClient

    async with BleakClient(address, timeout=30) as client:
        print_("Windows may show a pairing prompt: accept it, then confirm the number on the watch.")
        return bool(await client.pair())


MACOS_STEPS = (
    "macOS has no pairing API for apps: CoreBluetooth pairs on the first encrypted read.\n"
    "Run `wristconf finish-setup` with the watch in Pair Phone mode; if macOS shows a pairing\n"
    "prompt, accept it and confirm the number on the watch. Not yet tested with a Garmin watch:\n"
    "please report what happens (docs/PLATFORMS.md)."
)


def platform_name() -> str:
    return {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}.get(sys.platform, sys.platform)
