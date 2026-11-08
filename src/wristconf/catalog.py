"""Catalogs: the map of every settings screen of a watch model and firmware.

`build()` turns a snapshot into a catalog (JSON). A catalog built from your watch still holds your
values; `sanitize()` keeps only the structure (screens, items, types, choices) so it can be shared.
The sanitized catalogs bundled in `wristconf/catalogs/` power `wristconf find` and the docs.
"""

from __future__ import annotations

import copy
import json
import re
import time
from importlib import resources
from pathlib import Path

from google.protobuf import json_format, text_format

from wristconf.protocol import FOLLOW_TYPES, ROOT_SCREEN_ID, S, label
from wristconf.snapshot import load_screen

FORMAT = 1


# ── build ────────────────────────────────────────────────────────────────────────────────────────
def options_of(t) -> dict:
    o: dict = {}
    if t.HasField("options"):
        o["choices"] = [
            {"index": i, "label": label(e.title), **({"subtitle": label(e.subtitle)} if e.HasField("subtitle") else {})}
            for i, e in enumerate(t.options.option)
        ]
    if t.HasField("durationOptions"):
        d = t.durationOptions
        o["duration_s"] = {"min": d.minSeconds, "max": d.maxSeconds, "step": d.stepSize}
    if t.HasField("floatOptions"):
        f = t.floatOptions
        o["float"] = {"min": f.minValue, "max": f.maxValue, "step": f.stepSize}
    if t.HasField("integerPicker"):
        o["integer"] = json_format.MessageToDict(t.integerPicker, preserving_proto_field_name=True)
    if t.HasField("activity"):
        o["activity"] = S.Target.Activity.Name(t.activity)
    return o


def value_of(st, choices) -> str:
    """Current value as the watch shows it (used in snapshots; removed by sanitize)."""
    if st is None:
        return ""
    if st.HasField("switch"):
        return "On" if st.switch.enabled else "Off"
    if st.HasField("summary"):
        sm = st.summary
        if sm.HasField("valueList"):
            i = sm.valueList.index
            if choices and i < len(choices):
                return choices[i]["label"]
            return label(sm.title) or f"index {i}"
        if sm.HasField("valueInteger"):
            v = sm.valueInteger
            return label(v.formatted) or f"{v.value}{(' ' + label(v.unit)) if v.HasField('unit') else ''}"
        if sm.HasField("valueDuration"):
            d = sm.valueDuration
            return label(d.formatted) or label(d.title) or f"{d.seconds} s"
        if sm.HasField("valueFloat"):
            f = sm.valueFloat
            return label(f.formatted) or f"{f.value:g} {label(f.uom)}".strip()
        if label(sm.title):
            return label(sm.title)
        return text_format.MessageToString(sm, as_one_line=True)[:80]
    return ""


def build(snapshot: Path) -> dict:
    """Catalog of a snapshot directory, screens in depth-first order from the root."""
    raw = snapshot / "screens"
    cat: dict = {"format": FORMAT, "generated": time.strftime("%Y-%m-%d"), "sanitized": False,
                 "root": ROOT_SCREEN_ID, "screens": {}}
    seen: set[int] = set()

    def visit(sid: int, path: str) -> None:
        if sid in seen:
            return
        seen.add(sid)
        dresp, sresp, _ = load_screen(raw, sid)
        if dresp is None:
            return
        d = dresp.definition
        title = label(d.title) or "(untitled)"
        state_by_id = {x.id: x for x in sresp.state.state} if sresp else {}
        entries, kids = [], []
        for e in d.entry:
            t = e.target if e.HasField("target") else None
            o = options_of(t) if t is not None else {}
            st = state_by_id.get(e.id)
            ent = {
                "id": e.id,
                "label": label(e.title),
                "row": S.RowType.Name(e.rowType) if e.HasField("rowType") else None,
                "target": S.Target.Type.Name(t.type) if t is not None and t.HasField("type") else None,
                "subscreen": t.subscreen if t is not None and t.HasField("subscreen") else None,
                "value": value_of(st, o.get("choices")),
                "state_flag": st.state if st is not None and st.HasField("state") else None,
                **({"options": o} if o else {}),
            }
            if e.HasField("sortOptions"):
                ent["sort_entries"] = [label(x.title) for x in e.sortOptions.entries]
            if e.HasField("textOption"):
                ent["text_max"] = e.textOption.limits.maxLength
            entries.append(ent)
            if t is not None and t.type in FOLLOW_TYPES and t.HasField("subscreen"):
                kids.append((t.subscreen, f"{path} > {_step(ent['label'], e.id)}"))
        menus = [{"label": label(m.label),
                  "target": json_format.MessageToDict(m.target, preserving_proto_field_name=True)}
                 for m in d.menu_entry]
        kids += [(m.target.subscreen, f"{path} > {label(m.label)}") for m in d.menu_entry
                 if m.HasField("target") and m.target.type in FOLLOW_TYPES and m.target.HasField("subscreen")]
        cat["screens"][str(sid)] = {"title": title, "path": path, "status": dresp.status,
                                    "entries": entries, "menu": menus}
        for c, cpath in kids:
            visit(c, cpath)

    # Paths are made of ITEM names (what you select on the watch), which is what `navigate` follows.
    # Screen titles can differ: the item "Display & Brightness" opens a screen titled "Display".
    visit(ROOT_SCREEN_ID, "Settings")
    for f in sorted(raw.glob("*.def.bin")):  # read but no longer reachable from the root
        sid = int(f.name.split(".")[0])
        if sid not in seen:
            visit(sid, f"(orphan {sid})")
    cat.update(about(cat))
    return cat


def _step(item_label: str, entry_id: int) -> str:
    """Path step for an item: its name, or `#id` when the watch gives it none."""
    return item_label if item_label and ">" not in item_label else f"#{entry_id}"


def about(cat: dict) -> dict:
    """Model and firmware, read from Settings > System > About."""
    for sc in cat["screens"].values():
        if sc["path"].endswith("System > About") or sc["title"] == "About":
            notices = [e["label"] for e in sc["entries"] if e["row"] == "ROW_NOTICE" and e["label"]]
            model = notices[0] if notices else None
            fw = None
            if "Software Version" in notices:
                i = notices.index("Software Version")
                fw = notices[i + 1] if i + 1 < len(notices) else None
            return {"model": model, "firmware": fw}
    return {"model": None, "firmware": None}


# ── sanitize ─────────────────────────────────────────────────────────────────────────────────────
_SERIAL = re.compile(r"^\d{6,}$")
_DFU = re.compile(r"\[DFU-[0-9A-Fa-f]+\]")
# A notice that is only a number or a size is a value shown as text: Fitness Age ("41.0"), storage ("11.6 GB").
_MEASURE = re.compile(r"^\d+(\.\d+)?( ?[KMGT]B( Available)?)?$")
# Choice subtitles such as "+18 Hours" or "2h 0m" are battery estimates and times computed from the
# watch's current state: they differ per watch and per day.
_QUANTITY = re.compile(r"^[+-]?\d+\s?(Hours?|Days?)$|^\d+h \d+m$")
ENTRY_PERSONAL_KEYS = ("value", "state_flag", "sort_entries")
# Numeric limits can follow your own values (an LTHR picker tops out at your max heart rate; power
# zone pickers follow your FTP). The tool re-reads limits live before any write, so the map drops them.
NUMERIC_OPTION_KEYS = ("integer", "duration_s", "float")


def sanitize(cat: dict) -> dict:
    """Copy of a catalog with only structure: no values, states, list orders, numeric limits or serials."""
    out = copy.deepcopy(cat)
    for sc in out["screens"].values():
        about_screen = sc["path"].endswith("System > About") or sc["title"] == "About"
        for e in sc["entries"]:
            for k in ENTRY_PERSONAL_KEYS:
                e.pop(k, None)
            opts = e.get("options")
            if opts:
                for k in NUMERIC_OPTION_KEYS:
                    opts.pop(k, None)
                for ch in opts.get("choices", []):
                    if _QUANTITY.match(ch.get("subtitle", "")):
                        ch.pop("subtitle")
                if not opts:
                    e.pop("options")
            if about_screen or e["row"] == "ROW_NOTICE":
                if _SERIAL.match(e["label"]):
                    e["label"] = "<serial number>"
                elif not about_screen and _MEASURE.match(e["label"]):
                    e["label"] = "<value>"
                e["label"] = _DFU.sub("[DFU-<redacted>]", e["label"])
    out["sanitized"] = True
    return out


def labels(cat: dict) -> set[str]:
    out: set[str] = set()
    for sc in cat["screens"].values():
        out.add(sc["title"])
        for e in sc["entries"]:
            out.add(e["label"])
            for ch in e.get("options", {}).get("choices", []):
                out.add(ch["label"])
        out.update(m["label"] for m in sc["menu"])
    out.discard("")
    return out


def review_list(cat: dict, reference: list[dict]) -> list[str]:
    """Labels not found in any reference catalog: names you gave things (an activity you renamed,
    say) show up here. Read this list before sharing a catalog."""
    known: set[str] = set()
    for r in reference:
        known |= labels(r)
    return sorted(labels(cat) - known)


# ── bundled catalogs ─────────────────────────────────────────────────────────────────────────────
def bundled() -> list[Path]:
    root = resources.files("wristconf") / "catalogs"
    if not root.is_dir():
        return []

    def version(p: Path):  # fw-9.10 < fw-21.43 < fw-23.40
        return (p.parent.name, [int(x) if x.isdigit() else 0 for x in re.findall(r"\d+", p.stem)])

    return sorted((Path(str(p)) for p in root.rglob("*.json")), key=version)


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def slug(model: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (model or "unknown").lower()).strip("-")


# ── search ───────────────────────────────────────────────────────────────────────────────────────
def find(cat: dict, text: str, limit: int = 50) -> list[dict]:
    """Items whose label, screen path or choices contain `text` (case-insensitive)."""
    q = text.lower()
    hits = []
    for sc in cat["screens"].values():
        for e in sc["entries"]:
            if not e["label"] or e["row"] in ("ROW_SPACE", "ROW_NOTICE"):
                continue
            choices = [c["label"] for c in e.get("options", {}).get("choices", [])]
            if q in e["label"].lower() or any(q in c.lower() for c in choices):
                hits.append({"path": sc["path"], "item": e["label"], "row": e["row"], "target": e["target"],
                             "choices": choices})
                if len(hits) >= limit:
                    return hits
    return hits


# ── render ───────────────────────────────────────────────────────────────────────────────────────
def _md(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _opt_text(o: dict) -> str:
    parts = []
    if "choices" in o:
        parts.append(" · ".join(c["label"] for c in o["choices"]))
    if "duration_s" in o:
        d = o["duration_s"]
        parts.append(f"duration {d['min']}–{d['max']} s (step {d['step']})")
    if "float" in o:
        f = o["float"]
        parts.append(f"{f['min']:g}–{f['max']:g} (step {f['step']:g})")
    if "integer" in o:
        parts.append("integer " + json.dumps(o["integer"]))
    if "activity" in o:
        parts.append(f"opens in the phone app ({o['activity']})")
    return "; ".join(parts)


def render_markdown(cat: dict, title: str | None = None) -> str:
    """One section per screen, in tree order, with every item, its type and its choices."""
    model, fw = cat.get("model") or "Garmin watch", cat.get("firmware") or "?"
    show_values = not cat.get("sanitized", False)
    L = [f"# {title or f'Settings map: {model}, firmware {fw}'}", "",
         f"Generated by [wristconf](https://github.com/luisfelipecr/wristconf) from the Bluetooth "
         f"\"Realtime settings\" protocol, read-only. {len(cat['screens'])} screens.", "",
         "Each section is a screen (`screen N` is the id used on the wire; ids change between firmware "
         "versions, so wristconf navigates by path and name). Items marked ⚠️ run an action at once.", ""]
    root = cat["screens"].get(str(cat.get("root", ROOT_SCREEN_ID)))
    if root:
        L += ["## Contents", ""]
        L += [f"- [{e['label']}](#screen-{e['subscreen']})" for e in root["entries"] if e.get("subscreen")]
        L.append("")
    for sid, sc in cat["screens"].items():
        depth = sc["path"].count(" > ")
        status = "" if sc["status"] == 0 else f"  ❌ status {sc['status']}"
        heading = f"{'#' * min(2 + depth, 6)} {_md(sc['path'])}  `screen {sid}`{status}"
        L += [f"<a id=\"screen-{sid}\"></a>", heading, ""]
        rows = [e for e in sc["entries"] if e["row"] != "ROW_SPACE"]
        if rows:
            head = "| id | item | type |" + (" value |" if show_values else "") + " choices | goes to |"
            L += [head, "|---:|---|---|" + ("---|" if show_values else "") + "---|---|"]
            for e in rows:
                kind = (e["row"] or "").replace("ROW_", "").lower()
                if e["target"]:
                    kind += " / " + e["target"].replace("TYPE_", "").lower()
                if e["row"] == "ROW_ACTION":
                    kind = "⚠️ " + kind
                opts = _opt_text(e.get("options", {}))
                if e.get("sort_entries"):
                    opts = (opts + "; " if opts else "") + "sortable: " + " · ".join(e["sort_entries"])
                if e.get("text_max"):
                    opts = (opts + "; " if opts else "") + f"text, up to {e['text_max']} characters"
                go = f"[→ {e['subscreen']}](#screen-{e['subscreen']})" if e.get("subscreen") else ""
                val = f" {_md(str(e.get('value', '')))} |" if show_values else ""
                L.append(f"| {e['id']} | {_md(e['label'])} | {kind} |{val} {_md(opts)} | {go} |")
            L.append("")
        for m in sc["menu"]:
            sub = m["target"].get("subscreen")
            L.append(f"- menu **{_md(m['label'])}**" + (f" → [{sub}](#screen-{sub})" if sub else ""))
        if sc["menu"]:
            L.append("")
    return "\n".join(L) + "\n"


def render_markdown_split(cat: dict, outdir: Path) -> list[Path]:
    """One Markdown file per top-level area (and per activity), plus README.md as the index.

    Large single files do not render well on GitHub; this keeps each page small.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    groups: dict[str, dict] = {}
    for sid, sc in cat["screens"].items():
        parts = sc["path"].split(" > ")
        if len(parts) >= 4 and parts[1] == "Activities & Apps" and parts[2] == "Activities":
            key = f"Activities - {parts[3]}"
        else:
            key = parts[1] if len(parts) > 1 else "Settings"
        groups.setdefault(key, {})[sid] = sc
    files, index = [], []
    where = {sid: slug(k) + ".md" for k, g in groups.items() for sid in g}  # screen id -> page

    def relink(m: re.Match, page: str) -> str:  # links to screens on other pages point to that page
        target = where.get(m.group(1), "")
        return f"]({'' if target == page else target}#screen-{m.group(1)})"

    for key in sorted(groups):
        name = slug(key) + ".md"
        sub = {**cat, "screens": groups[key]}
        text = render_markdown(sub, title=f"{key}: {cat.get('model')}, firmware {cat.get('firmware')}")
        text = re.sub(r"\]\(#screen-(\d+)\)", lambda m, page=name: relink(m, page), text)
        (outdir / name).write_text(text, encoding="utf-8")
        files.append(outdir / name)
        index.append(f"- [{key}]({name}) · {len(groups[key])} screens")
    head = [f"# Settings map: {cat.get('model')}, firmware {cat.get('firmware')}", "",
            f"{len(cat['screens'])} screens read over Bluetooth by wristconf, read-only. Paths use item names, "
            "as `wristconf set` and settings files expect them.", ""]
    (outdir / "README.md").write_text("\n".join(head + index) + "\n", encoding="utf-8")
    return [outdir / "README.md", *files]
