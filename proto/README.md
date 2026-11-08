# Protocol buffer definitions

These `.proto` files come from [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)
(commit `c9fef95`, `app/src/main/proto/garmin/`), licensed under the AGPL-3.0. The full
`gdi_settings_service.proto` there has 668 lines; the copy embedded in `garmin-ble` is older and smaller.

One change was made: the protobuf `package` is renamed to `gbsettings`. Without the rename, these
messages collide in the protobuf descriptor pool with the ones `garmin-ble` registers itself.

The generated Python modules live in `src/wristconf/_proto/` and are committed. To regenerate them
after editing a `.proto`, run `scripts/gen_proto.sh` (needs `protoc`).
