"""wristconf command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from wristconf import __version__
from wristconf import catalog as cat_mod
from wristconf import device as dev
from wristconf.change import Change, ChangeError, Result, navigate, run_change
from wristconf.diff import compare, format_diff
from wristconf.pairing import MACOS_STEPS, WATCH_STEPS, platform_name

FREE_THE_WATCH = ("The watch talks to one phone at a time. Turn Bluetooth OFF on your phone in its Settings app "
                  "(an iPhone's Control Center toggle is not enough), then try again. Turn it back on when done.")


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────
class Logger:
    """Prints user-facing lines; keeps protocol chatter in a log file under the data folder."""

    def __init__(self, command: str):
        d = dev.data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{command}.log"
        self._f = self.path.open("a", encoding="utf-8")

    def _write(self, msg: str) -> None:
        self._f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        self._f.flush()

    def __call__(self, msg: str) -> None:
        print(msg, flush=True)
        self._write(msg)

    def quiet(self, msg: str) -> None:
        self._write(msg)

    def close(self) -> None:
        self._f.close()


def _session(args, log: Logger, level: int = 1):
    from wristconf.protocol import Session

    device = dev.resolve_device(args.device)
    return Session(device, log.quiet, level=level, capture=getattr(args, "capture_file", None))


async def _connect(sess, log: Logger) -> None:
    from garmin_ble.errors import GarminBleError

    try:
        await sess.connect()
    except GarminBleError as exc:
        raise SystemExit(f"Could not connect: {exc}\n{FREE_THE_WATCH}") from None
    log(f"connected to {sess.watch.info.name or sess.device}")


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit("refusing to write without a terminal to confirm; pass --yes to confirm in advance")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _load_catalog(p: str) -> dict:
    path = Path(p)
    if path.is_dir():
        f = path / "catalog.json"
        return cat_mod.load(f) if f.exists() else cat_mod.build(path)
    return cat_mod.load(path)


# ── commands ─────────────────────────────────────────────────────────────────────────────────────
def cmd_scan(args) -> int:
    rows = asyncio.run(dev.scan(args.timeout))
    if not rows:
        print("Nothing found. Is Bluetooth on? Is the watch free (phone Bluetooth off)?")
        return 1
    for r in rows:
        mark = "garmin" if r["garmin"] else "      "
        print(f"{mark}  {r['address']}  {r['rssi'] or 0:4d} dBm  {r['name']}")
    print("\nSave your watch as the default with: wristconf use <address>")
    return 0


def cmd_use(args) -> int:
    f = dev.save_device(args.device_id)
    print(f"default device: {args.device_id} (saved in {f})")
    return 0


def cmd_pair(args) -> int:
    device = dev.resolve_device(args.device)
    print(WATCH_STEPS, "\n")
    if sys.platform == "linux":
        if not device:
            raise SystemExit("give the watch address: wristconf pair --device AA:BB:CC:DD:EE:FF (see wristconf scan)")
        from wristconf.pairing import pair_linux

        ok = pair_linux(device)
    elif sys.platform == "win32":
        if not device:
            raise SystemExit("give the watch address: wristconf pair --device AA:BB:CC:DD:EE:FF (see wristconf scan)")
        from wristconf.pairing import pair_windows

        ok = asyncio.run(pair_windows(device))
    elif sys.platform == "darwin":
        print(MACOS_STEPS)
        return 0
    else:
        raise SystemExit(f"pairing is not implemented on {platform_name()}")
    if not ok:
        return 1
    if device:
        dev.save_device(device)
    print("Paired. Next, once: wristconf finish-setup")
    return 0


def cmd_finish_setup(args) -> int:
    """Tells the watch setup is complete, so it stops dropping the link after ~20 s."""
    log = Logger("finish-setup")

    async def run():
        sess = _session(args, log, level=2)
        try:
            await _connect(sess, log)
            await asyncio.sleep(5)
            log("setup marked complete. From now on the normal commands work.")
        finally:
            await sess.close()

    try:
        asyncio.run(run())
    finally:
        log.close()
    return 0


def cmd_info(args) -> int:
    from wristconf.protocol import label, read_screen

    log = Logger("info")

    async def run():
        sess = _session(args, log)
        try:
            await _connect(sess, log)
            sid = await navigate(sess, "Settings > System > About", log.quiet)
            d, _ = await read_screen(sess, sid)
            lines = [label(e.title) for e in d.entry if label(e.title)]
            for ln in lines:
                print(f"  {ln if args.show_serial else mask_identifiers(ln)}")
        finally:
            await sess.close()

    try:
        asyncio.run(run())
    finally:
        log.close()
    return 0


def mask_identifiers(line: str) -> str:
    """Hides the serial number and DFU hash: this output tends to get pasted into issues."""
    if cat_mod._SERIAL.match(line):
        return "<serial number hidden: use --show-serial>"
    return cat_mod._DFU.sub("[DFU-<hidden>]", line)


def _default_snapshot_dir(prefix: str) -> Path:
    return dev.data_dir() / "snapshots" / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}"


def cmd_snapshot(args) -> int:
    if args.base:
        return _snapshot_quick(args)
    out = Path(args.out) if args.out else _default_snapshot_dir("full")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Full snapshot into {out}\n"
          "This takes ~30 minutes on a fenix 8. It resumes where it stopped if the link drops.")
    last, stall = -1, 0
    cmd = [sys.executable, "-m", "wristconf", "_snapshot-worker", "--out", str(out)]
    if args.device:
        cmd += ["--device", args.device]
    for round_ in range(1, args.max_rounds + 1):
        code = subprocess.run(cmd).returncode  # one process per connection (see protocol.Session)
        done = json.loads((out / "state.json").read_text())["stats"]["screens"] if (out / "state.json").exists() else 0
        if code == 0:
            break
        if code not in (3,):
            print(f"stopped: worker exited with code {code}")
            return code
        stall = stall + 1 if done == last else 0
        last = done
        print(f"round {round_}: link dropped at {done} screens; reconnecting")
        if stall >= 3:
            print(f"no progress in 3 rounds. Run the same command again to resume: wristconf snapshot --out {out}")
            return 2
        time.sleep(4)
    else:
        print(f"gave up after {args.max_rounds} rounds; resume with: wristconf snapshot --out {out}")
        return 1
    return _finish_snapshot(out, base=None)


def _finish_snapshot(out: Path, base: Path | None) -> int:
    c = cat_mod.build(out)
    (out / "catalog.json").write_text(json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"done: {len(c['screens'])} screens · {c.get('model')} · firmware {c.get('firmware')}")
    print(f"  {out / 'catalog.json'}")
    print("  This snapshot holds YOUR values (weight, birth date, zones, serial). Keep it private.")
    if base is not None:
        text = format_diff(compare(_load_catalog(str(base)), c))
        (out / f"diff-{base.name}.txt").write_text(text, encoding="utf-8")
        print("\n" + text)
    return 0


def cmd_snapshot_worker(args) -> int:
    from wristconf.protocol import LinkLost
    from wristconf.snapshot import walk

    out = Path(args.out)
    log = Logger("snapshot")

    async def run() -> bool:
        sess = _session(args, log)
        try:
            await _connect(sess, log)
            return await walk(sess, out, log.quiet)
        finally:
            await sess.close()

    try:
        return 0 if asyncio.run(run()) else 3
    except LinkLost as exc:
        log.quiet(f"link lost: {exc}")
        return 3
    finally:
        log.close()


def _snapshot_quick(args) -> int:
    from wristconf.protocol import LinkLost
    from wristconf.snapshot import take_quick

    base = Path(args.base).resolve()
    if not (base / "screens").is_dir():
        raise SystemExit(f"{base} is not a snapshot folder")
    out = Path(args.out) if args.out else _default_snapshot_dir("quick")
    if (out / "screens").exists():
        raise SystemExit(f"{out} already exists; choose another --out")
    out.mkdir(parents=True, exist_ok=True)
    log = Logger("snapshot-quick")
    print(f"Quick snapshot over {base.name} into {out} (~9 minutes on a fenix 8; same firmware only)")

    async def run():
        sess = _session(args, log)
        try:
            await _connect(sess, log)
            return await take_quick(sess, base, out, log)
        finally:
            await sess.close()

    try:
        stats = asyncio.run(run())
    except LinkLost as exc:
        raise SystemExit(f"link lost ({exc}); run again with another --out") from None
    finally:
        log.close()
    print(f"{stats['screens']} screens · changed {len(stats['changed'])} · new {len(stats['new'])} · "
          f"gone {len(stats['gone'])} · failed {len(stats['failed'])}")
    return _finish_snapshot(out, base)


def cmd_diff(args) -> int:
    d = compare(_load_catalog(args.old), _load_catalog(args.new))
    print(format_diff(d, args.max), end="")
    return 0


def cmd_show(args) -> int:
    from wristconf.change import describe
    from wristconf.protocol import FOLLOW_TYPES, S, is_denied, label, read_screen

    log = Logger("show")

    async def show(sess, sid, title, depth, indent, seen):
        d, state = await read_screen(sess, sid)
        print(f"{indent}### {title}  (screen {sid}, '{label(d.title)}')")
        subs = []
        for e in d.entry:
            row = S.RowType.Name(e.rowType) if e.HasField("rowType") else "?"
            tname = S.Target.Type.Name(e.target.type) if e.HasField("target") and e.target.HasField("type") else ""
            st = state.get(e.id)
            flag = f" state={st.state}" if st is not None and st.HasField("state") else ""
            cur = describe(e, st) if st is not None else ""
            line = f"{indent}  [{e.id}] {label(e.title)!r} {row} {tname}{flag}"
            if cur and cur != "(?)":
                line += f"  = {cur}"
            if e.HasField("target") and e.target.HasField("subscreen"):
                line += f"  -> {e.target.subscreen}"
                if e.target.type in FOLLOW_TYPES:
                    subs.append((e.target.subscreen, label(e.title)))
            print(line)
            if tname == "TYPE_LIST" and e.target.HasField("options"):
                print(f"{indent}      choices: {[label(o.title) for o in e.target.options.option]}")
        for m in d.menu_entry:
            print(f"{indent}  (menu) {label(m.label)!r}")
        if depth > 0:
            for sub, name in subs:
                if sub in seen or is_denied(sub, name):
                    continue
                seen.add(sub)
                await show(sess, sub, f"{title} > {name}", depth - 1, indent + "    ", seen)

    async def run():
        sess = _session(args, log)
        try:
            await _connect(sess, log)
            sid = await navigate(sess, args.path, log.quiet)
            await show(sess, sid, args.path, args.depth, "", {sid})
        finally:
            await sess.close()

    try:
        asyncio.run(run())
    except ChangeError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        log.close()
    return 0


def cmd_find(args) -> int:
    paths = [Path(args.catalog)] if args.catalog else cat_mod.bundled()
    if not paths:
        raise SystemExit("no catalog: pass --catalog")
    catalog = _load_catalog(str(paths[-1]))  # the newest bundled firmware sorts last
    hits = cat_mod.find(catalog, args.text, args.limit)
    print(f"{catalog.get('model')} · firmware {catalog.get('firmware')} · {len(hits)} match(es)\n")
    for h in hits:
        kind = (h["row"] or "").replace("ROW_", "").lower() + (
            " / " + h["target"].replace("TYPE_", "").lower() if h["target"] else "")
        print(f"{h['path']} › {h['item']}  [{kind}]")
        if h["choices"]:
            print(f"    choices: {' · '.join(h['choices'])}")
    return 0 if hits else 1


def _change_from_args(args) -> Change:
    d = {k: getattr(args, k) for k in ("path", "item", "choice", "int", "text", "time", "duration", "float",
                                       "move_to", "entry_id", "check")}
    d = {k: v for k, v in d.items() if v is not None}
    if args.switch is not None:
        d["switch"] = args.switch
    if args.select:
        d["select"] = True
    if args.remove:
        d["remove"] = True
    return Change.from_dict(d)


def _run_changes(args, changes: list[Change], title: str) -> int:
    """Simulate every change; then, with --apply and a yes, write them in order, stopping at the first failure."""
    log = Logger("apply" if args.apply else "plan")

    offline = getattr(args, "offline", None)

    async def run() -> int:
        if offline:
            from wristconf.snapshot import SnapshotSession

            sess = SnapshotSession(Path(offline))
            print(f"offline: simulating against the snapshot {offline} (values as of when it was taken)")
        else:
            sess = _session(args, log)
            await _connect(sess, log)
        try:
            print(f"\n{title}: {len(changes)} change(s), simulating first\n")
            sims: list[Result] = []
            structural = False  # an earlier change adds, renames or reorders things
            for c in changes:
                try:
                    sims.append(await run_change(sess, c, apply=False, log=log, quiet=log.quiet))
                except ChangeError as exc:
                    if not structural:
                        log(f"  ✗ {exc}")
                        print("\nStopped: fix the change above. Nothing was written.")
                        return 2
                    # e.g. "rename HIIT" after "add HIIT": it can only be checked once the add is written
                    log(f"  … depends on earlier changes; checked again at write time ({exc})")
                    sims.append(Result(c, "pending", new=c.describe_value()))
                structural = structural or c.kind() in ("select", "text", "move_to", "remove")
            todo = [r for r in sims if r.status in ("simulated", "pending")]
            print(f"\nplan: {len(todo)} to change, {len(sims) - len(todo)} already set")
            for r in todo:
                cur = r.current if r.status == "simulated" else "(after the changes above)"
                print(f"  {r.change.path} › {r.change.item}: {cur} → {r.new}")
            if not args.apply or not todo:
                if todo:
                    print("\nSimulation only. Add --apply to write these changes.")
                return 0
            if not _confirm(f"\nWrite {len(todo)} change(s) to the watch?", args.yes):
                print("Nothing was written.")
                return 1
            for n, r in enumerate(todo, 1):
                try:
                    res = await run_change(sess, r.change, apply=True, log=log, quiet=log.quiet)
                except ChangeError as exc:
                    log(f"  ✗ {exc}")
                    res = None
                if res is None or not res.ok:
                    print(f"\nStopped at change {n}/{len(todo)}. Changes before it were written and verified.")
                    return 4
            print(f"\nDone: {len(todo)} change(s) written and verified.")
            return 0
        finally:
            await sess.close()

    try:
        return asyncio.run(run())
    finally:
        print(f"log: {log.path}")
        log.close()


def cmd_set(args) -> int:
    try:
        c = _change_from_args(args)
    except ChangeError as exc:
        raise SystemExit(str(exc)) from None
    return _run_changes(args, [c], "set")


def cmd_apply(args) -> int:
    from wristconf.plan import load

    try:
        preset = load(Path(args.file))
    except ChangeError as exc:
        raise SystemExit(str(exc)) from None
    if preset.description:
        print(preset.description)
    return _run_changes(args, preset.changes, preset.name)


def cmd_check(args) -> int:
    from wristconf.plan import check, load

    try:
        preset = load(Path(args.file))
    except ChangeError as exc:
        raise SystemExit(str(exc)) from None
    path = Path(args.catalog) if args.catalog else (cat_mod.bundled() or [None])[-1]
    if path is None:
        raise SystemExit("no catalog: pass --catalog")
    catalog = _load_catalog(str(path))
    problems = check(preset, catalog)
    print(f"{preset.name}: {len(preset.changes)} change(s) checked against "
          f"{catalog.get('model')} firmware {catalog.get('firmware')}")
    for p in problems:
        print(f"  ✗ {p}")
    if not problems:
        print("  no problems found (availability per activity is only known on the watch: run plan)")
    return 1 if problems else 0


def cmd_catalog(args) -> int:
    if args.catalog_cmd == "list":
        for p in cat_mod.bundled():
            c = cat_mod.load(p)
            print(f"{c.get('model')} · firmware {c.get('firmware')} · {len(c['screens'])} screens  ({p.name})")
        return 0
    if args.catalog_cmd == "build":
        c = cat_mod.build(Path(args.snapshot))
        Path(args.output).write_text(json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{len(c['screens'])} screens → {args.output} (holds your values: sanitize before sharing)")
        return 0
    if args.catalog_cmd == "sanitize":
        c = cat_mod.sanitize(_load_catalog(args.input))
        Path(args.output).write_text(json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")
        review = cat_mod.review_list(c, [cat_mod.load(p) for p in cat_mod.bundled()])
        print(f"sanitized: {len(c['screens'])} screens → {args.output}")
        if review:
            print(f"\nBefore sharing, read these {len(review)} labels that no bundled catalog has. Names you gave "
                  "things (a renamed activity, say) would appear here:")
            for lab in review[:300]:
                print(f"  {lab}")
        return 0
    if args.catalog_cmd == "render":
        c = _load_catalog(args.input)
        if args.split:
            files = cat_mod.render_markdown_split(c, Path(args.output))
            print(f"{len(c['screens'])} screens → {len(files)} files in {args.output}")
            return 0
        Path(args.output).write_text(cat_mod.render_markdown(c), encoding="utf-8")
        print(f"{len(c['screens'])} screens → {args.output}")
        return 0
    return 2


# ── parser ───────────────────────────────────────────────────────────────────────────────────────
def _add_value_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--choice", help="pick an option of a list")
    g.add_argument("--switch", choices=["on", "off"])
    g.add_argument("--int", type=int)
    g.add_argument("--text", help="text field (e.g. Rename)")
    g.add_argument("--time", help="time of day, HH:MM")
    g.add_argument("--duration", type=int, help="duration in seconds")
    g.add_argument("--float", type=float)
    g.add_argument("--select", action="store_true", help="pick an action row on a selection screen (Add, Options…)")
    g.add_argument("--move-to", type=int, help="sortable list: new position (0 = top)")
    g.add_argument("--remove", action="store_true", help="sortable list: remove the entry")
    p.add_argument("--entry-id", type=int, help="item id, when two items share a name")
    p.add_argument("--check", help='"PATH|ITEM|VALUE" to verify elsewhere after writing')


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="wristconf", description="Garmin watch settings over Bluetooth, from a computer.")
    ap.add_argument("--version", action="version", version=f"wristconf {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    def watch_cmd(name, help_, fn):
        p = sub.add_parser(name, help=help_) if help_ else sub.add_parser(name)  # no help = not listed
        p.add_argument("--device", help="address (MAC, or UUID on macOS) or part of the name; default: saved one")
        p.set_defaults(fn=fn)
        return p

    p = sub.add_parser("scan", help="list nearby Bluetooth devices")
    p.add_argument("--timeout", type=float, default=12.0)
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("use", help="save the default watch")
    p.add_argument("device_id", metavar="DEVICE")
    p.set_defaults(fn=cmd_use)
    watch_cmd("pair", "pair this computer with the watch as a new phone", cmd_pair)
    watch_cmd("finish-setup", "once after pairing: mark setup complete on the watch", cmd_finish_setup)
    p = watch_cmd("info", "model and firmware versions (Settings > System > About)", cmd_info)
    p.add_argument("--show-serial", action="store_true")

    p = watch_cmd("snapshot", "read every settings screen into a local folder (read-only)", cmd_snapshot)
    p.add_argument("--out", help="snapshot folder (an existing one resumes)")
    p.add_argument("--base", help="earlier snapshot of the same firmware: re-read states only, then diff (~9 min)")
    p.add_argument("--max-rounds", type=int, default=80, help=argparse.SUPPRESS)
    p = watch_cmd("_snapshot-worker", None, cmd_snapshot_worker)
    p.add_argument("--out", required=True)

    p = sub.add_parser("diff", help="compare two catalogs or snapshots by path")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--max", type=int, default=200, help="lines per section")
    p.set_defaults(fn=cmd_diff)

    p = watch_cmd("show", "read one screen live (read-only)", cmd_show)
    p.add_argument("path", help='e.g. "Settings > Activities & Apps > Activities > Run"')
    p.add_argument("--depth", type=int, default=0, help="also read N levels of sub-screens")

    p = sub.add_parser("find", help="search the settings map (no watch needed)")
    p.add_argument("text")
    p.add_argument("--catalog", help="catalog JSON or snapshot folder (default: newest bundled)")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_find)

    p = watch_cmd("set", "change one setting (simulates unless --apply)", cmd_set)
    p.add_argument("path")
    p.add_argument("item", help="exact item name on that screen")
    _add_value_args(p)
    p.add_argument("--apply", action="store_true", help="write to the watch (asks first)")
    p.add_argument("--yes", action="store_true", help="do not ask before writing")

    p = sub.add_parser("check", help="check a settings file against the settings map (no watch needed)")
    p.add_argument("file")
    p.add_argument("--catalog", help="catalog JSON or snapshot folder (default: newest bundled)")
    p.set_defaults(fn=cmd_check)

    for name, help_ in (("plan", "simulate a settings file (TOML)"), ("apply", "write a settings file (asks first)")):
        p = watch_cmd(name, help_, cmd_apply)
        p.add_argument("file")
        p.set_defaults(apply=name == "apply")
        if name == "apply":
            p.add_argument("--yes", action="store_true", help="do not ask before writing")
        else:
            p.add_argument("--offline", metavar="SNAPSHOT", help="simulate against a snapshot, without the watch")
            p.set_defaults(yes=False)

    p = sub.add_parser("catalog", help="build, sanitize, render or list catalogs (no watch needed)")
    p.set_defaults(fn=cmd_catalog)
    csub = p.add_subparsers(dest="catalog_cmd", required=True, metavar="action")
    csub.add_parser("list", help="bundled catalogs")
    q = csub.add_parser("build", help="catalog of a snapshot (keeps your values)")
    q.add_argument("snapshot")
    q.add_argument("-o", "--output", required=True)
    q = csub.add_parser("sanitize", help="structure only, safe to share")
    q.add_argument("input")
    q.add_argument("-o", "--output", required=True)
    q = csub.add_parser("render", help="Markdown map of a catalog")
    q.add_argument("input")
    q.add_argument("-o", "--output", required=True, help="file, or folder with --split")
    q.add_argument("--split", action="store_true", help="one file per area, plus an index")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows pipes default to cp1252, which cannot print "→"
        if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    if getattr(args, "apply", None) is None:
        args.apply = False
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
