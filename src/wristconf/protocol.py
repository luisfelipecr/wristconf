"""Connection to the watch: GFDI handshake and the "Realtime settings" protobuf service.

Built on garmin-ble (transport, framing, GFDI). This module adds what the settings service needs:
reassembly of protobuf responses split in chunks, matching responses by content (the watch answers
with its own request ids), Gadgetbridge's protobuf definitions and the handshake replies the watch
expects from a "phone".

Safety rails live here, below the CLI, so no caller can skip them:
- a ChangeRequest is refused unless the caller passes ``allow_change=True``;
- only a fixed list of system events can be sent (FACTORY_RESET is not on it);
- OAuth requests from the watch are logged and never answered.
"""

from __future__ import annotations

import asyncio
import platform
import struct
import time
import warnings
from collections.abc import Callable
from pathlib import Path

from garmin_ble.constants import GarminMessage
from garmin_ble.errors import ConnectionFailed
from garmin_ble.protocol import gfdi
from garmin_ble.transport.ble import BleTransport
from garmin_ble.watch import Watch

from wristconf._proto.garmin import gdi_settings_service_pb2 as S
from wristconf._proto.garmin.gdi_smart_proto_pb2 import Smart

__all__ = ["S", "Smart", "Session", "LinkLost", "SettingsWatch", "ROOT_SCREEN_ID", "FOLLOW_TYPES", "label",
           "is_denied", "send_event", "EV_TIME_UPDATED"]

Log = Callable[[str], None]

# bleak's BlueZ backend warns on every connection when the MTU is read before being acquired.
warnings.filterwarnings("ignore", message="Using default MTU value")

ROOT_SCREEN_ID = 36352
FOLLOW_TYPES = {S.Target.TYPE_SUBSCREEN, S.Target.TYPE_SUBSCREEN_OPTIONS}
REALTIME_SETTINGS_CAPABILITY_BIT = 92

# Screens never requested. Connect IQ screens: a fenix 8 rebooted (blue triangle) with screen 2166
# (Run > Alerts > Connect IQ) in flight; 4294902234 is the untitled id next to it.
DENY_SCREEN_IDS = {2166, 4294902234}


def is_denied(screen_id: int, path: str) -> bool:
    return screen_id in DENY_SCREEN_IDS or "Connect IQ" in path


def label(lbl) -> str:
    """Text of a protobuf Label (falls back to its id when the watch sends no text)."""
    return (lbl.text or lbl.id or "").strip()


# ── system events ────────────────────────────────────────────────────────────────────────────────
EV_SYNC_COMPLETE, EV_PAIR_COMPLETE, EV_SYNC_READY, EV_SETUP_WIZARD_COMPLETE, EV_TIME_UPDATED = 0, 4, 8, 14, 16
ALLOWED_EVENTS = frozenset({EV_SYNC_COMPLETE, EV_PAIR_COMPLETE, EV_SYNC_READY, EV_SETUP_WIZARD_COMPLETE,
                            EV_TIME_UPDATED})
EV_FACTORY_RESET = 2  # same enum. Never sent.


def check_event(ev: int) -> None:
    # An explicit raise, not an assert: asserts disappear under `python -O`.
    if ev not in ALLOWED_EVENTS or ev == EV_FACTORY_RESET:
        raise PermissionError(f"system event {ev} is not on the allow list")


async def send_event(w: Watch, ev: int, log: Log) -> None:
    check_event(ev)
    log(f"TX system_event {ev}")
    await w._send_gfdi(gfdi.GfdiMessageBuilder.build_system_event(ev, 0))


async def init_level(w: Watch, level: int, log: Log) -> None:
    """1 = Gadgetbridge's routine (time, file types, SYNC_READY).

    2 = also tells the watch that pairing and setup are complete. Needed once per new "phone":
    until then the watch shows "finish setup in app" and drops the link after ~20 s.
    """
    if level >= 1:
        await send_event(w, EV_TIME_UPDATED, log)
        log("TX supported_file_types_request (5031)")
        await w._send_gfdi(gfdi.GfdiMessageBuilder.build_message(5031, b""))
        await send_event(w, EV_SYNC_READY, log)
    if level >= 2:
        for ev in (EV_PAIR_COMPLETE, EV_SYNC_COMPLETE, EV_SETUP_WIZARD_COMPLETE):
            await send_event(w, ev, log)


# ── GFDI helpers ─────────────────────────────────────────────────────────────────────────────────
def status_response(ref_type: int, payload: bytes) -> bytes:
    """GFDI 5000 with a payload (a "rich" reply to a message from the watch)."""
    return gfdi._build_frame(5000, struct.pack("<Hb", ref_type, 0) + payload)


def pack_string(s: str) -> bytes:
    b = s.encode()[:255]
    return bytes([len(b)]) + b


_orig_parse_message = gfdi.parse_message


def _parse_message_compact(frame: bytes):
    """Normalizes compact GFDI types (bit 0x8000 -> (t & 0xff) + 5000), as Gadgetbridge does."""
    m = _orig_parse_message(frame)
    if m is not None and m.type & 0x8000:
        m = gfdi.GfdiMessage(type=(m.type & 0xFF) + 5000, payload=m.payload, raw=m.raw)
    return m


# After the handshake the watch switches to compact message types, which garmin-ble 0.3.2 does not
# decode. Patched at import time; harmless for messages that are not compact.
gfdi.parse_message = _parse_message_compact


class SettingsWatch(Watch):
    """garmin-ble's Watch plus chunk reassembly, content matching and the settings service."""

    def __init__(self, transport, *, handshake: bool, log: Log, host_name: str = "wristconf"):
        super().__init__(transport, heartbeat=None, reconnect="off")
        self.sw_handshake = handshake
        self.sw_log = log
        self.sw_host_name = host_name
        self.sw_chunks: dict[tuple[int, int], bytearray] = {}
        self.sw_pending: dict = {}
        self.sw_req = 0x4000
        self.sw_caps: bytes | None = None
        self.sw_unsolicited: list[dict] = []
        self.sw_write_lock = asyncio.Lock()

    # ── GFDI messages ────────────────────────────────────────────────────────────────────────────
    def _on_gfdi_message(self, message: gfdi.GfdiMessage) -> None:
        t = message.type
        self.sw_log(f"RX gfdi {t} len={len(message.payload)}")
        if self.sw_handshake and t == GarminMessage.DEVICE_INFORMATION:
            proto = struct.unpack_from("<H", message.payload, 0)[0] if len(message.payload) >= 2 else 150
            resp = struct.pack("<HH", proto, 0xFFFF) + struct.pack("<I", 0xFFFFFFFF)
            resp += struct.pack("<HH", 7791, 0xFFFF)
            resp += pack_string(self.sw_host_name) + pack_string(platform.system() or "Desktop")
            resp += pack_string("wristconf")
            resp += bytes([0])  # 0 = no compact GFDI types requested
            self._schedule(self._reflex_gfdi(status_response(t, resp)))
            self._on_device_information(message)
            return
        if self.sw_handshake and t == GarminMessage.CONFIGURATION:
            self._schedule(self._reflex_gfdi(gfdi.GfdiMessageBuilder.build_status_ack(t)))
            n = message.payload[0] if message.payload else 0
            self.sw_caps = bytes(message.payload[1 : 1 + n])
            # echo the watch's own capabilities back: promise nothing beyond them
            self._schedule(self._reflex_gfdi(gfdi.GfdiMessageBuilder.build_message(
                int(GarminMessage.CONFIGURATION), bytes([n]) + self.sw_caps)))
            return
        if self.sw_handshake and t == 5036:  # NOTIFICATION_SUBSCRIPTION
            enable = message.payload[0] if len(message.payload) > 0 else 0
            unk = message.payload[1] if len(message.payload) > 1 else 0
            self._schedule(self._reflex_gfdi(status_response(t, bytes([0, enable, unk]))))
            return
        if self.sw_handshake and t == GarminMessage.CURRENT_TIME_REQUEST:
            ref = struct.unpack_from("<I", message.payload, 0)[0] if len(message.payload) >= 4 else 0
            now = int(time.time())
            resp = struct.pack("<IIiii", ref, now - gfdi.GARMIN_EPOCH_OFFSET, time.localtime(now).tm_gmtoff, 0, 0)
            self._schedule(self._reflex_gfdi(status_response(t, resp)))
            return
        if self.sw_handshake and t == GarminMessage.AUTH_NEGOTIATION:
            self._schedule(self._reflex_gfdi(status_response(t, struct.pack("<BI", 0, 0))))
            return
        if t == GarminMessage.CONFIGURATION:
            n = message.payload[0] if message.payload else 0
            self.sw_caps = bytes(message.payload[1 : 1 + n])
        if t in (GarminMessage.PROTOBUF_REQUEST, GarminMessage.PROTOBUF_RESPONSE):
            self._on_protobuf(message)  # only the protobuf ACK (Gadgetbridge does not send the generic one too)
            return
        super()._on_gfdi_message(message)

    async def _send_gfdi(self, frame: bytes) -> None:
        # Serialize frames. With MTU 23 each frame becomes several 20-byte writes, and ACKs scheduled
        # in parallel interleaved their pieces (the watch answered LENGTH_ERROR).
        async with self.sw_write_lock:
            await super()._send_gfdi(frame)

    async def _reflex_gfdi(self, frame: bytes) -> None:
        try:
            await super()._reflex_gfdi(frame)
        except ConnectionFailed:
            pass  # an ACK for a frame that arrived while the link was closing: worthless, not an error

    def has_realtime_settings(self) -> bool | None:
        if self.sw_caps is None:
            return None
        bit = REALTIME_SETTINGS_CAPABILITY_BIT
        return len(self.sw_caps) > bit // 8 and bool(self.sw_caps[bit // 8] & (1 << (bit % 8)))

    # ── protobuf with reassembly ─────────────────────────────────────────────────────────────────
    def _on_protobuf(self, message: gfdi.GfdiMessage) -> None:
        frame = gfdi.parse_protobuf_frame(message)
        if frame is None:
            self.sw_log("RX malformed protobuf frame")
            return
        self._schedule(self._reflex_gfdi(gfdi.GfdiMessageBuilder.build_protobuf_ack(
            message.type, frame.request_id, frame.data_offset)))

        chunk_key = (message.type, frame.request_id)
        if frame.data_offset == 0 and len(frame.proto_bytes) >= frame.total_length:
            body = bytes(frame.proto_bytes)
        else:
            buf = self.sw_chunks.setdefault(chunk_key, bytearray())
            if frame.data_offset != len(buf):
                self.sw_log(f"chunk out of order req={frame.request_id} off={frame.data_offset} have={len(buf)}")
                self.sw_chunks.pop(chunk_key, None)
                return
            buf.extend(frame.proto_bytes)
            if len(buf) < frame.total_length:
                return
            body = bytes(self.sw_chunks.pop(chunk_key))

        smart = Smart()
        try:
            smart.ParseFromString(body)
        except Exception as exc:  # noqa: BLE001
            self.sw_log(f"undecodable protobuf: {exc}")
            return
        fields = [f.name for f, _ in smart.ListFields()]
        self.sw_log(f"RX smart req={frame.request_id} type={message.type} fields={fields}")

        # The watch answers with PROTOBUF_REQUEST and its own request id: match by content.
        key = response_key(smart, self._sw_single_pending)
        fut = self.sw_pending.pop(key, None) if key else None
        if fut is None:
            fut = self.sw_pending.pop(frame.request_id, None)
        if fut is not None:
            if not fut.done():
                fut.set_result((smart, body))
        else:
            # A request the watch started on its own (OAuth and friends): logged, never answered.
            self.sw_unsolicited.append({"request_id": frame.request_id, "type": int(message.type), "fields": fields})

    def _sw_single_pending(self, kind: str):
        """Failure responses carry no screenId: match them with the only pending request of that kind."""
        keys = [k for k in self.sw_pending if isinstance(k, tuple) and k[0] == kind]
        return keys[0] if len(keys) == 1 else None

    async def sw_request(self, settings: S.SettingsService, timeout: float = 15.0, allow_change: bool = False):
        """Sends one settings-service request and waits for its response. Read-only unless allow_change."""
        key = request_key(settings, allow_change)
        smart = Smart()
        smart.settings_service.CopyFrom(settings)
        body = smart.SerializeToString()
        self.sw_req = (self.sw_req + 1) % 65536
        fut = asyncio.get_running_loop().create_future()
        self.sw_pending[key] = fut
        await self._send_gfdi(gfdi.GfdiMessageBuilder.build_protobuf_request(
            request_id=self.sw_req, data_offset=0, total_length=len(body), proto_bytes=body))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.sw_pending.pop(key, None)


def request_key(settings: S.SettingsService, allow_change: bool):
    """Matching key for an outgoing request. Raises for anything that is not an allowed request."""
    if settings.HasField("changeRequest"):
        if not allow_change:
            raise PermissionError("ChangeRequest needs allow_change=True")
        return ("change", settings.changeRequest.screenId)
    if settings.HasField("definitionRequest"):
        return ("def", settings.definitionRequest.screenId)
    if settings.HasField("stateRequest"):
        return ("state", settings.stateRequest.screenId)
    if settings.HasField("initRequest"):
        return ("init", 0)
    raise PermissionError("only definition, state, init and (with allow_change) change requests are sent")


def response_key(smart: Smart, single_pending: Callable[[str], object]):
    if not smart.HasField("settings_service"):
        return None
    ss = smart.settings_service
    if ss.HasField("definitionResponse"):
        dr = ss.definitionResponse
        return ("def", dr.definition.screenId) if dr.HasField("definition") else single_pending("def")
    if ss.HasField("stateResponse"):
        sr = ss.stateResponse
        return ("state", sr.state.screenId) if sr.HasField("state") else single_pending("state")
    if ss.HasField("initResponse"):
        return ("init", 0)
    if ss.HasField("changeResponse"):
        cr = ss.changeResponse
        return ("change", cr.state.screenId) if cr.HasField("state") else single_pending("change")
    return None


class LinkLost(Exception):
    """The Bluetooth link dropped. Start a new process to reconnect (see Session)."""


def transport_for(device: str | None, connect_timeout: float = 30.0) -> BleTransport:
    """`device` is an address (Linux/Windows MAC, macOS UUID) or part of the advertised name."""
    from wristconf.device import looks_like_address

    if device and looks_like_address(device):
        return BleTransport(address=device, connect_timeout=connect_timeout)
    return BleTransport(name_pattern=device, connect_timeout=connect_timeout)


class Session:
    """One connection per process.

    Reconnecting inside the same process leaves old bleak clients listening, which duplicates and
    corrupts messages (seen up to 9x). When the link drops, LinkLost is raised; the caller saves its
    progress and a new process resumes.
    """

    def __init__(self, device: str | None, log: Log, *, handshake: bool = True, level: int = 1,
                 keepalive: float = 10.0, capture: Path | None = None):
        self.device, self.log = device, log
        self.handshake, self.level, self.keepalive = handshake, level, keepalive
        self.capture = capture
        self.watch: SettingsWatch | None = None
        self.connected_at: float | None = None
        self.nav_cache: dict | None = None
        self._ka_task: asyncio.Task | None = None

    async def connect(self) -> None:
        w = SettingsWatch(transport_for(self.device), handshake=self.handshake, log=self.log)
        if self.capture is not None:
            w.record(self.capture)  # raw frames: contains personal data, opt-in only
        self.log(f"connecting to {self.device or 'the first Garmin watch found'}")
        await w._connect()
        self.watch = w
        self.log(f"connected: {w.info}")
        await asyncio.sleep(3.0)  # DEVICE_INFORMATION / CONFIGURATION arrive now
        if w.has_realtime_settings() is False:
            self.log("warning: this watch does not advertise the Realtime Settings capability (bit 92); "
                     "settings requests will probably go unanswered")
        self.connected_at = time.monotonic()
        if self.level:
            await init_level(w, self.level, self.log)
        if self.keepalive:
            self._ka_task = asyncio.ensure_future(self._keepalive())

    async def _keepalive(self) -> None:
        while self.watch and self.watch.is_connected:
            await asyncio.sleep(self.keepalive)
            try:
                await send_event(self.watch, EV_TIME_UPDATED, self.log)
            except Exception:  # noqa: BLE001
                return

    async def close(self) -> None:
        if self._ka_task:
            self._ka_task.cancel()
        if self.watch is not None:
            try:
                await self.watch._disconnect()
            except Exception:  # noqa: BLE001
                pass

    @property
    def unsolicited(self) -> list[dict]:
        return self.watch.sw_unsolicited if self.watch else []

    async def request(self, req: S.SettingsService, timeout: float = 15.0):
        """Read request (definition/state). Returns (Smart, raw bytes)."""
        return await self._send(req, timeout, allow_change=False)

    async def change(self, req: S.SettingsService, timeout: float = 15.0):
        """Write request. Only change.py calls this, after the user confirmed."""
        return await self._send(req, timeout, allow_change=True)

    async def _send(self, req, timeout, allow_change):
        if self.watch is None or not self.watch.is_connected:
            raise LinkLost("no link")
        try:
            return await self.watch.sw_request(req, timeout=timeout, allow_change=allow_change)
        except TimeoutError:
            if self.watch.is_connected:
                raise
            raise LinkLost("link dropped during the request") from None
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001  (bleak / NotConnected)
            raise LinkLost(repr(exc)) from None


def definition_request(screen_id: int, locale: str = "en_US") -> S.SettingsService:
    req = S.SettingsService()
    req.definitionRequest.screenId = screen_id
    req.definitionRequest.unk2 = 0
    req.definitionRequest.locale = locale
    return req


def state_request(screen_id: int) -> S.SettingsService:
    req = S.SettingsService()
    req.stateRequest.screenId = screen_id
    return req


async def read_screen(sess: Session, sid: int):
    """Definition + state of one screen. Returns (ScreenDefinition, {entry id: EntryState})."""
    # System > Shortcuts (7 lists x 52 options) takes longer than 15 s on a fenix 8.
    ds, _ = await sess.request(definition_request(sid), timeout=45)
    try:
        ss, _ = await sess.request(state_request(sid), timeout=8)
        state = {x.id: x for x in ss.settings_service.stateResponse.state.state}
    except TimeoutError:
        state = {}
    return ds.settings_service.definitionResponse.definition, state
