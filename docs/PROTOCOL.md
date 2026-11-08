# The Realtime Settings protocol: what was learned on a real watch

Notes from reading and writing the settings of a **fenix 8 (47 mm)**, firmware 21.43 and 23.40, from a
Linux computer, October 2026. They complement Gadgetbridge's implementation. "Verified" means observed on
the watch; everything else is labeled as a hypothesis.

Base: GFDI v2 (multi-link `6A4E2800…`, COBS + CRC16) from [`garmin-ble`](https://github.com/gwerneckp/garmin-ble)
0.3.2; protobuf definitions from [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) `c9fef95`
(`gdi_settings_service.proto`, 668 lines; the copy in garmin-ble is older).

## Connection and pairing

- The watch accepts **one BLE client at a time**. While paired with a phone that is connected, it advertises
  only to that phone.
- It can keep **two "phones"** paired (a phone and the computer). For the computer to connect, turn
  Bluetooth **off in the phone's Settings app**. On an iPhone, the Control Center toggle only disconnects
  for a while: the watch stayed in "Syncing" and "Pair Phone" would not open.
- Pairing the computer as a new phone: on the watch, Connectivity → Phone → Pair Phone (the QR code must
  **stay** on screen), then numeric comparison. Confirm the passkey on the watch quickly: waiting makes the
  watch cancel.
- After pairing, BlueZ stays connected: disconnect before the first real session.
- Each new phone leaves the watch in **"finish setup in app"**, and it **drops the client after ~20 s**
  ("Remote User Terminated Connection 0x13" in btmon). Fix: send `PAIR_COMPLETE` (4), `SYNC_COMPLETE` (0) and
  `SETUP_WIZARD_COMPLETE` (14) once (`wristconf finish-setup`). After that, the normal routine is enough.

## Handshake

- Answer `DEVICE_INFORMATION` (5024) and echo `CONFIGURATION` (5050). Capability `REALTIME_SETTINGS` = bit 92.
- After the handshake the watch uses **compact GFDI types**: bit `0x8000` set → real type = `(t & 0xff) + 5000`.
- Routine: `TIME_UPDATED` (16), `SupportedFileTypes` request (5031), `SYNC_READY` (8); keepalive
  `TIME_UPDATED` every 10 s keeps the link stable.
- Answer `NOTIFICATION_SUBSCRIPTION` (5036) with a status, and `CURRENT_TIME_REQUEST` (5052) **echoing the
  reference id**.

## Settings service: reading

- Root screen `36352`. `ScreenDefinitionRequest{screenId, unk2=0, locale}` + `ScreenStateRequest{screenId}`.
- The watch answers with **`PROTOBUF_REQUEST` and a request id of its own**: match responses by content
  (`definitionResponse.definition.screenId` / `stateResponse.state.screenId`). A failure response
  (`status: 2`) carries **no screenId**: match it with the only pending request of that kind.
- Large responses come in 375-byte pieces (an activity profile is ~6 KB): reassemble by (type, request id).
- Navigation targets: `TYPE_SUBSCREEN` (0), `TYPE_SUBSCREEN_OPTIONS` (9) and `menu_entry`. Ids ≥ 0x80000000
  are **legitimate** (per-day sleep times, "Custom Data" layouts).
- Reading the definition of "Add Data Screen → Custom Data → N fields" **does not create** a screen (verified).
- `EntryState.state`: **1 = unavailable in this activity or context** (verified: Power, Running Dynamics and
  Diving fields on a HIIT-based activity; screens already added in "Add Data Screen"). **2 = disabled / read-only**
  (Gadgetbridge greys the row out): ready-made data screens, and the `Status` of a Focus Mode turned on
  automatically.
- The **state lists every item of the definition** (verified on 669 of 689 screens), so "did a screen gain or
  lose items?" can be answered from states alone. `wristconf snapshot --base` relies on this.
- **Screen ids are stable within a firmware**, across sessions and after customizing (verified). **Between
  firmware versions they are not**: from 21.43 to 23.40 only 4 ids stayed the same. Navigate by path and item
  name, never by stored ids.
- **Screen ids are shared across activities** (an alert screen reached from several activities has one id). Values
  read there can depend on which path opened it last, so they may differ between two reads (observed on
  `Alerts > Add New` screens; the cause is a hypothesis).
- Paths are made of **item names**, which can differ from screen titles: the item "Display & Brightness" opens
  a screen titled "Display".
- Cost of a full read on a fenix 8: definitions ≈ 650 KB, states ≈ 120 KB for ~690 screens → ~29 min; states
  only ~9 min. The bottleneck is BLE throughput, not latency. On Linux, garmin-ble reports MTU 23 and writes
  20-byte pieces, but 23 is bleak's default when the MTU has not been acquired (bleak warns about it): the MTU
  actually negotiated was not measured, and a larger one could make this faster (hypothesis).
- `System > Shortcuts` answers, but its definition (7 lists × 52 options) takes more than 15 s: allow 45 s.

## Settings service: writing (verified on firmware 23.40)

- `ChangeRequest{screenId, entryId, <value>}` → `changeResponse{status: SUCCESS|GENERIC_ERROR, state,
  shouldReturn?, unk2?}`; match by `("change", state.screenId)`. Values: `option{index}` (list; index in the
  order of `target.options`), `switch{value}`, `integerValue{value}`, `text{value}`, `timeOfDay{seconds}`
  (seconds since 00:00), `duration{seconds}`, `floatValue{value}`; an action (`ROW_ACTION`) is just
  `{screenId, entryId}`.
- `unk2` carries **alerts the watch shows** (title / message / OK). Example: writing the lactate threshold heart
  rate → "Running Threshold Auto Detection Disabled" (the watch turns that detection off by itself).
- Chained effects: raising *Max. Heart Rate* **rescales the LTHR**; the LTHR picker's maximum follows the
  current max heart rate.
- Adding an activity: `ROW_ACTION` on "… > Activities > Add" → `shouldReturn: true`; the new activity appears
  under Activities with the name of its type ("HIIT"). `Rename` (`ROW_TEXT`, ≤ 127 characters) changes the name
  and therefore the path.
- **Data field:** in `… > Edit Screen > Field N` (screen title "Data Fields") a `TYPE_LIST` category takes
  `option{index}`; categories that open an "Options" screen use `ROW_ACTION` → `shouldReturn: true`.
- **Data screen layout:** a `TYPE_LIST` item whose name is the current layout ("1 Field", "4 Fields 3 Rows"…);
  its state comes in `EntryState.unk8.unk1` (index), not in `summary`.
- **Context:** data-field screen ids repeat across data screens (every "Field 1" of an activity has the same
  id). The watch applies the change to the **last "Edit Screen" opened** (verified). Always navigate the whole
  path right before writing, and re-read the Edit Screen afterwards.
- **Ready-made screen** (HR Zone Gauge, Map…): `ROW_ACTION` on "Add Data Screen" (title "Choose Screen") →
  added at the end of the list.
- **Sortable list** (`ROW_SORTABLE_AND_DELETABLE`: Data Screens > Edit, Glances > Edit, Controls, Pinned):
  `entryId` = **the SortEntry id** (not the row's); move = `position{index}` (absolute target, 0 = top);
  remove = `position{delete: true}`. `SortEntry.unk5 = 0` marks an entry that cannot be removed (Power Off).
  "Available" items (`ROW_SINGLE_ACTION`) join the list with `{screenId, entryId}`.
- **An active Focus Mode changes the menus:** with the "Activity" focus mode on (automatic, `state=2`),
  `Notifications & Alerts` and `Display & Brightness` show the mode's overrides ("Don't Change"), under other
  screen ids, and the general value is hidden (`state=1`). Check `Focus Modes > * > Status` when in doubt.
- **Changing units (imperial → metric) keeps the number and swaps the unit:** auto lap 1 mi → 1 km, alerts
  2.5 mi → 2.5 km. Review lap and alert distances after changing units.
- **Not exposed:** choosing the main watch face, pairing ANT+/BLE sensors ("go to the Sensors & Accessories menu
  on your Garmin device").

## Not solved yet: menus without a target

`MenuEntry.unk1` holds an id (Save 5, Cancel 6, Remove from List 7) but no target. Without it there is no way to
save a **new** custom data screen, copy an activity or use "Remove from List". Tried without effect:
`ChangeRequest{screenId, entryId=unk1}` → `GENERIC_ERROR`; `SettingsService.unk11{unk1}` → no answer; the
unnamed `ChangeRequest` fields 9 and 13 (sub-messages: no answer or `GENERIC_ERROR`) and 10 (varint 5 / true →
`GENERIC_ERROR`). Gadgetbridge master (`66d2852`, 2026-10-16) does not implement menus either.

Likely next step (hypothesis): capture the Garmin Connect app doing "Add Data Screen" / "Remove". On iOS, the
"Bluetooth for iOS" logging profile plus `idevicebtlogger`, or PacketLogger on a Mac, gives a decrypted HCI log.

## Implementation traps

- **Serialize writes.** With MTU 23 each frame becomes several 20-byte writes; ACKs sent in parallel
  interleaved their pieces → `LENGTH_ERROR` (`5000 ffff05`) and the state never arrived.
- **One ACK per protobuf** (garmin-ble 0.3.2 sent the generic and the specific one).
- **One process per connection.** Reconnecting in the same process leaves old bleak clients listening →
  duplicated (up to 9×) and corrupted messages.
- **Connect IQ screens are blocked.** The watch rebooted (blue triangle) with screen 2166 (Run > Alerts >
  Connect IQ) in flight. It came back by itself, but wristconf never requests those screens.
- `pkill -f <pattern>` kills your own shell when the pattern is on the same command line: kill by PID.
