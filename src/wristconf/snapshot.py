"""Snapshots: reading the whole settings tree of the watch, read-only.

A snapshot directory holds, per screen, the raw protobuf definition and state (`screens/<id>.def.bin`,
`.state.bin`) and a readable `<id>.txt`. `catalog.build()` turns it into `catalog.json`.

Two ways to take one:
- full (`walk`): walks the tree from the root. ~30 min on a fenix 8 (~700 screens over BLE).
  Resumable: progress is in `state.json`, one process per connection.
- quick (`take_quick`): re-reads only the STATE of each screen of a previous snapshot from the same
  firmware, and re-reads a definition only where items changed. ~9 min.

A snapshot contains your personal values (weight, birth date, zones, serial number). Never publish
one: publish `wristconf catalog sanitize` output instead.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

from google.protobuf import text_format

from wristconf.protocol import (
    FOLLOW_TYPES,
    ROOT_SCREEN_ID,
    LinkLost,
    S,
    Session,
    Smart,
    definition_request,
    is_denied,
    label,
    state_request,
)

MAX_ATTEMPTS = 3


def _write_screen(raw: Path, sid: int, path: str, dbody: bytes, sbody: bytes) -> None:
    (raw / f"{sid}.def.bin").write_bytes(dbody)
    (raw / f"{sid}.state.bin").write_bytes(sbody)
    (raw / f"{sid}.txt").write_text(
        f"# path: {path}\n# definition\n" + text_format.MessageToString(parse(dbody).settings_service)
        + "\n# state\n" + text_format.MessageToString(parse(sbody).settings_service), encoding="utf-8")


def parse(raw: bytes) -> Smart:
    s = Smart()
    if raw:
        s.ParseFromString(raw)
    return s


def children(definition, path: str):
    """(screen id, path) of the sub-screens a definition links to."""
    for e in definition.entry:
        t = e.target
        if e.HasField("target") and t.type in FOLLOW_TYPES and t.HasField("subscreen"):
            yield t.subscreen, f"{path} > {label(e.title)}"
    for m in definition.menu_entry:
        if m.HasField("target") and m.target.type in FOLLOW_TYPES and m.target.HasField("subscreen"):
            yield m.target.subscreen, f"{path} > menu:{label(m.label)}"


# ── full snapshot ────────────────────────────────────────────────────────────────────────────────
def load_progress(out: Path):
    f = out / "state.json"
    if f.exists():
        st = json.loads(f.read_text())
        return deque(tuple(x) for x in st["queue"]), set(st["seen"]), st["stats"]
    stats = {"screens": 0, "failed": [], "skipped": [], "no_state": [], "attempts": {}}
    return deque([(ROOT_SCREEN_ID, "Settings")]), set(), stats


def save_progress(out: Path, queue, seen, stats) -> None:
    (out / "state.json").write_text(json.dumps({"queue": list(queue), "seen": sorted(seen), "stats": stats}))


async def walk(sess: Session, out: Path, log, max_screens: int = 5000) -> bool:
    """Reads screens until the queue is empty. True = complete. Raises LinkLost (progress is saved)."""
    queue, seen, stats = load_progress(out)
    raw = out / "screens"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        while queue and stats["screens"] < max_screens:
            sid, path = queue.popleft()
            if sid in seen:
                continue
            if is_denied(sid, path):
                log(f"skipped for safety: {path} ({sid})")
                stats["skipped"].append({"screen": sid, "path": path})
                seen.add(sid)
                continue
            key = str(sid)
            stats["attempts"][key] = stats["attempts"].get(key, 0) + 1
            if stats["attempts"][key] > MAX_ATTEMPTS:
                log(f"giving up on {path} ({sid}) after {MAX_ATTEMPTS} attempts")
                stats["failed"].append({"screen": sid, "path": path, "why": f"{MAX_ATTEMPTS} attempts"})
                seen.add(sid)
                continue
            try:
                dsmart, dbody = await sess.request(definition_request(sid), timeout=45)
            except TimeoutError:
                log(f"timeout on {path} ({sid}); back to the end of the queue")
                queue.append((sid, path))
                continue
            except LinkLost:
                queue.appendleft((sid, path))
                raise
            try:
                _, sbody = await sess.request(state_request(sid), timeout=10)
            except TimeoutError:
                stats["no_state"].append(sid)
                sbody = b""
            except LinkLost:
                queue.appendleft((sid, path))
                raise
            seen.add(sid)
            _write_screen(raw, sid, path, dbody, sbody)
            stats["screens"] += 1
            dresp = dsmart.settings_service.definitionResponse
            if dresp.status != 0:
                stats["failed"].append({"screen": sid, "path": path, "why": f"status {dresp.status}"})
            queue.extend(children(dresp.definition, path))
            log(f"{stats['screens']:4d} {path} ({len(dresp.definition.entry)} items, {len(queue)} queued)")
            save_progress(out, queue, seen, stats)
            await asyncio.sleep(0.05)
    finally:
        save_progress(out, queue, seen, stats)
    return not queue


# ── quick snapshot ───────────────────────────────────────────────────────────────────────────────
def _state_items(raw: bytes) -> list[bytes]:
    """State entries without the header (its unk2/unk3 change every session)."""
    return [x.SerializeToString(deterministic=True) for x in parse(raw).settings_service.stateResponse.state.state]


def _state_ids(raw: bytes) -> set[int]:
    return {x.id for x in parse(raw).settings_service.stateResponse.state.state}


async def take_quick(sess: Session, base: Path, out: Path, log) -> dict:
    """New snapshot in `out`, reusing definitions from `base` (same firmware) where nothing changed.

    A definition is re-read when the screen is new, its set of state items differs from the base,
    or the state request failed. When a screen's state changed, its parent is re-read too (the row
    title shown in the parent may carry the value).
    """
    braw, oraw = base / "screens", out / "screens"
    oraw.mkdir(parents=True, exist_ok=True)
    stats: dict = {"state_reads": 0, "def_reads": 0, "changed": [], "new": [], "failed": [], "skipped": 0}
    defs: dict[int, bytes] = {}
    states: dict[int, bytes] = {}
    paths: dict[int, str] = {}
    fresh: set[int] = set()

    async def read_def(sid: int) -> bool:
        try:
            smart, body = await sess.request(definition_request(sid), timeout=45)
        except TimeoutError:
            return False
        stats["def_reads"] += 1
        if smart.settings_service.definitionResponse.status != 0:
            return False
        defs[sid] = body
        fresh.add(sid)
        return True

    async def read_state(sid: int, timeout: float) -> tuple[bytes, bool]:
        try:
            smart, body = await sess.request(state_request(sid), timeout=timeout)
        except TimeoutError:
            return b"", False
        stats["state_reads"] += 1
        return body, smart.settings_service.stateResponse.state.screenId == sid

    def defn(sid: int):
        return parse(defs[sid]).settings_service.definitionResponse.definition

    queue: deque = deque([(ROOT_SCREEN_ID, "Settings", None)])
    seen: set[int] = set()
    while queue:
        sid, path, parent = queue.popleft()
        if sid in seen:
            continue
        seen.add(sid)
        if is_denied(sid, path):
            stats["skipped"] += 1
            continue
        paths[sid] = path
        bdef, bstate = braw / f"{sid}.def.bin", braw / f"{sid}.state.bin"
        known = bdef.exists()
        sbody, ok = await read_state(sid, 10.0)
        if known and ok and _state_ids(sbody) == _state_ids(bstate.read_bytes()):
            defs[sid] = bdef.read_bytes()
        elif not await read_def(sid):
            stats["failed"].append(path)
            log(f"failed: {path} ({sid})")
            continue
        if not ok:  # big screens can be slow: one more try with a longer deadline
            sbody, _ = await read_state(sid, 45.0)
        states[sid] = sbody
        if not known:
            stats["new"].append(path)
            log(f"new: {path}")
        elif _state_items(sbody) != _state_items(bstate.read_bytes()):
            stats["changed"].append(path)
            log(f"changed: {path}")
            if parent is not None and parent not in fresh and await read_def(parent):
                queue.extend((c, cp, parent) for c, cp in children(defn(parent), paths[parent]) if c not in seen)
        queue.extend((c, cp, sid) for c, cp in children(defn(sid), path))
        await asyncio.sleep(0.02)

    for sid, body in defs.items():
        _write_screen(oraw, sid, paths[sid], body, states.get(sid, b""))
    stats["screens"] = len(defs)
    stats["gone"] = sorted(int(p.name.split(".")[0]) for p in braw.glob("*.def.bin")
                           if int(p.name.split(".")[0]) not in defs)
    (out / "quick-stats.json").write_text(json.dumps({**stats, "base": str(base)}, indent=1))
    return stats


def screen_path(raw: Path, sid: int) -> str:
    txt = raw / f"{sid}.txt"
    if txt.exists():
        first = txt.read_text(encoding="utf-8").splitlines()[0]
        if first.startswith("# path: "):
            return first[8:]
    return ""


def load_screen(raw: Path, sid: int):
    """(DefinitionResponse, StateResponse, path) from a snapshot, or (None, None, None)."""
    d = raw / f"{sid}.def.bin"
    if not d.exists():
        return None, None, None
    st = raw / f"{sid}.state.bin"
    sbody = st.read_bytes() if st.exists() else b""
    return (parse(d.read_bytes()).settings_service.definitionResponse,
            parse(sbody).settings_service.stateResponse, screen_path(raw, sid))


__all__ = ["walk", "take_quick", "load_screen", "children", "parse", "S"]


class SnapshotSession:
    """Answers reads from a snapshot instead of the watch: simulate a settings file offline.

    Values are those of the moment the snapshot was taken. Writes are refused.
    """

    def __init__(self, snapshot: Path):
        self.raw = Path(snapshot) / "screens"
        if not self.raw.is_dir():
            raise FileNotFoundError(f"{snapshot} is not a snapshot folder")
        self.nav_cache = None

    async def request(self, req, timeout: float = 15.0):
        if req.HasField("definitionRequest"):
            f = self.raw / f"{req.definitionRequest.screenId}.def.bin"
        elif req.HasField("stateRequest"):
            f = self.raw / f"{req.stateRequest.screenId}.state.bin"
        else:
            raise PermissionError("a snapshot answers only definition and state requests")
        if not f.exists():
            raise TimeoutError  # the same thing the watch does for a screen it does not answer
        body = f.read_bytes()
        return parse(body), body

    async def change(self, req, timeout: float = 15.0):
        raise PermissionError("offline: a snapshot cannot be written")

    async def close(self) -> None:
        pass
