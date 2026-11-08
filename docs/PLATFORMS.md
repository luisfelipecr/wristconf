# Platforms

wristconf talks to the watch through [bleak](https://github.com/hbldh/bleak), which has backends for BlueZ
(Linux), CoreBluetooth (macOS) and WinRT (Windows). The protocol code is the same everywhere. What differs is
how you **pair** the computer with the watch and how the watch is **addressed**.

| | Linux | macOS | Windows |
|---|---|---|---|
| Status | **tested** (fenix 8, firmware 21.43 and 23.40) | beta, not yet tested with a watch | experimental, not yet tested with a watch |
| Watch address | MAC (`AA:BB:…`) | a UUID per computer (macOS hides MACs) | MAC |
| Pairing | `wristconf pair` (drives `bluetoothctl`) | system prompt on first connection (hypothesis) | `wristconf pair` (bleak's WinRT pairing) or Settings → Bluetooth |
| Expected speed | ~30 min full snapshot | unknown | unknown |

## Linux

1. `wristconf scan` and note the watch's address.
2. On the watch: Connectivity → Phone → Pair Phone, QR code on screen. Phone Bluetooth off in its Settings app.
3. `wristconf pair --device AA:BB:CC:DD:EE:FF`, then confirm the passkey on the watch right away.
4. `wristconf finish-setup` once. From then on, every command works with the phone's Bluetooth off.

## macOS (help wanted)

macOS gives apps no pairing API: CoreBluetooth starts pairing on the first encrypted read. The expected flow is
`wristconf scan` (shows a UUID), put the watch in Pair Phone mode, `wristconf use <UUID>`,
`wristconf finish-setup`, accept the macOS prompt and confirm the number on the watch. The terminal app needs
Bluetooth permission (System Settings → Privacy & Security → Bluetooth).

If you try it, please open an issue with what happened, even if it failed: that is how this row becomes "tested".

## Windows (help wanted)

`wristconf pair --device AA:BB:…` asks bleak to pair, and Windows should show a prompt. Pairing from
Settings → Bluetooth & devices may also work. Bluetooth inside a virtual machine is unreliable: use a real
Windows computer. Reports welcome, as for macOS.

## Watches

Tested: **fenix 8 47 mm**. Other Garmin watches that advertise the Realtime Settings capability (bit 92) may
work, since Gadgetbridge uses the same protocol for many models. The settings tree differs per model and
firmware, so read it first (`wristconf snapshot`) and share a sanitized catalog (see CONTRIBUTING.md).
