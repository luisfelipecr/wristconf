"""Changing settings: navigate by path and item name, simulate, then (only when asked) write and verify.

Every change goes through the same steps:
1. navigate to the screen by path (names, never stored ids: ids change between firmware versions);
2. read the item live and check the requested value against its type and limits;
3. refuse anything on the block list (destructive names, actions the watch asks to confirm,
   Connect IQ screens, items the watch marks unavailable);
4. without `apply`: report current → new and stop (simulation);
5. with `apply`: send one ChangeRequest, report any alert the watch shows, re-read and compare.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields

from google.protobuf import text_format

from wristconf.protocol import ROOT_SCREEN_ID, S, Session, is_denied, label, read_screen

DESTRUCTIVE = re.compile(r"restore|reset|delete|remove|clear|erase|factory|default", re.I)
# Screens where ROW_ACTION / ROW_SINGLE_ACTION means "pick this option", identified by the screen title.
SELECT_TITLES = {"Options", "Choose Screen", "Edit Glances", "Edit Controls", "Controls", "Edit Apps",
                 "Edit Activities", "Add"}
# Screens whose content depends on the last screen opened (data-field screens share ids across data
# screens): their parent is always re-read, never skipped by the navigation cache.
CONTEXT_TITLES = {"Edit Screen", "Data Fields", "Options", "Map"}
VALUE_KINDS = ("choice", "switch", "int", "text", "time", "duration", "float", "select", "move_to", "remove")


class ChangeError(Exception):
    """The change was refused or failed; nothing (more) was sent."""


@dataclass
class Change:
    path: str
    item: str
    choice: str | None = None
    switch: bool | None = None
    int: int | None = None
    text: str | None = None
    time: str | None = None        # HH:MM
    duration: int | None = None    # seconds
    float: float | None = None
    select: bool = False           # ROW_ACTION / ROW_SINGLE_ACTION on a selection screen
    move_to: int | None = None     # sortable list: new position, 0 = top
    remove: bool = False           # sortable list: remove the entry
    entry_id: int | None = None    # disambiguates items with the same name
    check: str | None = None       # "PATH|ITEM|VALUE" to verify elsewhere after writing

    def kind(self) -> str:
        # select/remove are flags (only True counts); switch=False is a real value ("off")
        set_kinds = [k for k in VALUE_KINDS
                     if (getattr(self, k) is True if k in ("select", "remove") else getattr(self, k) is not None)]
        if len(set_kinds) != 1:
            raise ChangeError(f"{self.path} › {self.item}: give exactly one value "
                              f"({', '.join(VALUE_KINDS)}), got {set_kinds or 'none'}")
        return set_kinds[0]

    def describe_value(self) -> str:
        k = self.kind()
        v = getattr(self, k)
        if k == "switch":
            return "On" if v else "Off"
        if k in ("select", "remove"):
            return k
        return f"{k} {v}"

    @classmethod
    def from_dict(cls, d: dict) -> Change:
        known = {f.name for f in fields(cls)}
        extra = set(d) - known
        if extra:
            raise ChangeError(f"unknown keys {sorted(extra)} (known: {sorted(known)})")
        d = dict(d)
        if isinstance(d.get("switch"), str):
            if d["switch"].lower() not in ("on", "off"):
                raise ChangeError(f"switch must be on/off or true/false, got {d['switch']!r}")
            d["switch"] = d["switch"].lower() == "on"
        if "path" not in d or "item" not in d:
            raise ChangeError(f"each change needs path and item: {d}")
        c = cls(**d)
        c.kind()
        return c


@dataclass
class Result:
    change: Change
    status: str                 # simulated | unchanged | applied | mismatch
    screen_id: int = 0
    current: str = ""
    new: str = ""
    request: str = ""
    alerts: list[str] = field(default_factory=list)
    verified: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("simulated", "unchanged", "applied")


def describe(e, st) -> str:
    """Current value of an entry, as text."""
    if st is None:
        return "(no state)"
    if st.HasField("switch"):
        return "On" if st.switch.enabled else "Off"
    if st.HasField("summary"):
        sm = st.summary
        if sm.HasField("valueList") and e.target.HasField("options"):
            i = sm.valueList.index
            opts = e.target.options.option
            return label(opts[i].title) if i < len(opts) else f"index {i}"
        if sm.HasField("valueInteger"):
            return label(sm.valueInteger.formatted) or str(sm.valueInteger.value)
        if sm.HasField("valueTimeOfDay"):
            s = sm.valueTimeOfDay.seconds
            return f"{s // 3600:02d}:{s % 3600 // 60:02d}"
        if sm.HasField("valueDuration"):
            return f"{sm.valueDuration.seconds}s ({label(sm.valueDuration.formatted)})"
        if sm.HasField("valueFloat"):
            return f"{sm.valueFloat.value:g} ({label(sm.valueFloat.formatted)})"
        return label(sm.title) or text_format.MessageToString(sm, as_one_line=True)
    if st.HasField("unk8") and e.HasField("target") and e.target.HasField("options"):
        i = st.unk8.unk1  # data screen layout: the chosen option index comes in unk8
        opts = e.target.options.option
        return label(opts[i].title) if i < len(opts) else f"index {i}"
    return "(?)"


def _leading_int(s: str) -> int | None:
    m = re.match(r"\s*(-?\d+)", s)
    return int(m.group(1)) if m else None


def same_value(kind: str, current: str, change: Change, new: str) -> bool:
    """True when the watch already has the requested value (the change is skipped)."""
    if kind == "int":
        return _leading_int(current) == change.int
    if kind in ("duration", "float"):
        return current.split(" ")[0] == new
    if kind in ("choice", "switch", "time", "text"):
        return current == new
    return False


def split_path(path: str) -> list[str]:
    parts = [p.strip() for p in path.split(">") if p.strip()]
    return parts[1:] if parts and parts[0] == "Settings" else parts


async def navigate(sess: Session, path: str, log=lambda m: None) -> int:
    """Screen id at `path` ("Settings > A > B"). `#N` in a path picks an item by id."""
    if is_denied(0, path):
        raise ChangeError("Connect IQ screens are blocked for safety")
    parts = split_path(path)
    if sess.nav_cache is None:
        sess.nav_cache = {(): (ROOT_SCREEN_ID, None)}  # path -> (screen id, title); this connection only
    cache = sess.nav_cache
    sid, trail = ROOT_SCREEN_ID, ["Settings"]
    for i, name in enumerate(parts):
        pkey, ckey = tuple(parts[:i]), tuple(parts[: i + 1])
        ptitle = cache.get(pkey, (None, None))[1]
        if ckey in cache and ptitle is not None and ptitle not in CONTEXT_TITLES:
            sid = cache[ckey][0]
            trail.append(name)
            continue
        d, st = await read_screen(sess, sid)
        cache[pkey] = (sid, label(d.title))
        linked = [e for e in d.entry if e.HasField("target") and e.target.HasField("subscreen")]
        if name.startswith("#") and name[1:].isdigit():
            hits = [e for e in linked if e.id == int(name[1:])]
        else:
            hits = [e for e in linked if label(e.title) == name]
        if len(hits) > 1:  # same name twice: drop the copy the watch marks unavailable (state=1)
            hits = [e for e in hits if not (e.id in st and st[e.id].state == 1)]
        hits += [m for m in d.menu_entry if label(m.label) == name and m.target.HasField("subscreen")]
        if len(hits) != 1:
            avail = [label(e.title) for e in linked]
            raise ChangeError(f"'{name}' not found (or ambiguous) in {' > '.join(trail)}. Available: {avail}")
        sid = hits[0].target.subscreen
        trail.append(name)
        log(f"  {' > '.join(trail)} → screen {sid}")
        prev = cache.get(ckey)
        cache[ckey] = (sid, prev[1] if prev and prev[0] == sid else None)
    if is_denied(sid, path):
        raise ChangeError("this screen is blocked for safety")
    return sid


def _find_entry(d, state, c: Change, kind: str):
    """(entry, sort entry or None, current order or None)."""
    if kind in ("move_to", "remove"):
        rows = [x for x in d.entry if x.HasField("sortOptions")]
        found = [(x, se) for x in rows for se in x.sortOptions.entries if label(se.title) == c.item]
        if len(found) != 1:
            avail = [label(se.title) for x in rows for se in x.sortOptions.entries]
            raise ChangeError(f"entry '{c.item}' not found (or ambiguous) in the sortable lists. Present: {avail}")
        e, se = found[0]
        return e, se, [label(x.title) for x in e.sortOptions.entries]
    hits = [x for x in d.entry if label(x.title) == c.item]
    if c.entry_id is not None:
        hits = [x for x in hits if x.id == c.entry_id]
    if len(hits) > 1:
        hits = [x for x in hits if not (state.get(x.id) is not None and state[x.id].state == 1)]
    if len(hits) != 1:
        raise ChangeError(f"item '{c.item}' not found (or ambiguous: use entry_id). "
                          f"Items: {[(x.id, label(x.title)) for x in d.entry]}")
    e = hits[0]
    if e.id in state and state[e.id].state == 1:
        raise ChangeError(f"'{c.item}' is unavailable on this screen (state=1)")
    return e, None, None


def build_request(sid: int, e, sort_entry, order, c: Change, kind: str, screen_title: str, log):
    """(SettingsService with a ChangeRequest, text of the new value). Validates first."""
    row = S.RowType.Name(e.rowType)
    tname = S.Target.Type.Name(e.target.type) if e.HasField("target") else None
    if DESTRUCTIVE.search(c.item) and kind != "remove":
        raise ChangeError(f"blocked: '{c.item}' has a destructive name")
    if row in ("ROW_ACTION", "ROW_SINGLE_ACTION") and kind != "select":
        raise ChangeError(f"'{c.item}' is {row}: use select")
    if e.HasField("action") and e.action.HasField("unk1"):
        mm = e.action.unk1
        raise ChangeError(f"blocked: the watch asks to confirm this action "
                          f"('{label(mm.title)}': '{label(mm.message)}')")

    req = S.SettingsService()
    cr = req.changeRequest
    cr.screenId, cr.entryId = sid, e.id
    if kind == "choice":
        if tname != "TYPE_LIST":
            raise ChangeError(f"'{c.item}' is not a list ({row}/{tname})")
        opts = [label(o.title) for o in e.target.options.option]
        if c.choice not in opts:
            raise ChangeError(f"'{c.choice}' is not a choice. Choices: {opts}")
        cr.option.index = opts.index(c.choice)
        new = c.choice
    elif kind == "switch":
        if row != "ROW_SWITCH":
            raise ChangeError(f"'{c.item}' is not a switch ({row})")
        cr.switch.value = bool(c.switch)
        new = "On" if c.switch else "Off"
    elif kind == "select":
        if row not in ("ROW_ACTION", "ROW_SINGLE_ACTION"):
            raise ChangeError(f"'{c.item}' is not ROW_ACTION/ROW_SINGLE_ACTION ({row})")
        if not (re.search(r">\s*Add$", c.path.strip()) or screen_title in SELECT_TITLES):
            raise ChangeError(f"select works only on selection screens {sorted(SELECT_TITLES)} "
                              f"(this one: '{screen_title}')")
        new = f"(select '{c.item}' on '{screen_title}')"
    elif kind == "text":
        if row != "ROW_TEXT":
            raise ChangeError(f"'{c.item}' is not text ({row})")
        mx = e.textOption.limits.maxLength if e.HasField("textOption") else 0
        if not c.text.strip() or (mx and len(c.text) > mx):
            raise ChangeError(f"text is empty or longer than {mx} characters")
        cr.text.value = c.text
        new = c.text
    elif kind == "time":
        if tname != "TYPE_TIME_OF_DAY":
            raise ChangeError(f"'{c.item}' is not a time of day ({row}/{tname})")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", c.time)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ChangeError("invalid time (HH:MM)")
        cr.timeOfDay.seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        new = f"{int(m.group(1)):02d}:{m.group(2)}"
    elif kind == "duration":
        if tname != "TYPE_DURATION":
            raise ChangeError(f"'{c.item}' is not a duration ({row}/{tname})")
        d = e.target.durationOptions
        if e.target.HasField("durationOptions") and d.maxSeconds and not d.minSeconds <= c.duration <= d.maxSeconds:
            raise ChangeError(f"{c.duration}s is outside the watch's limits ({d.minSeconds}–{d.maxSeconds}s)")
        cr.duration.seconds = c.duration
        new = f"{c.duration}s"
    elif kind == "float":
        if tname != "TYPE_FLOAT":
            raise ChangeError(f"'{c.item}' is not a decimal ({row}/{tname})")
        f = e.target.floatOptions
        if e.target.HasField("floatOptions") and f.maxValue and not f.minValue <= c.float <= f.maxValue:
            raise ChangeError(f"{c.float:g} is outside the watch's limits ({f.minValue:g}–{f.maxValue:g})")
        cr.floatValue.value = c.float
        new = f"{c.float:g}"
    elif kind in ("move_to", "remove"):
        cr.entryId = sort_entry.id  # sortable lists take the id of the SortEntry, not of the row
        if kind == "remove":
            if sort_entry.HasField("unk5") and sort_entry.unk5 == 0:
                raise ChangeError(f"'{c.item}' cannot be removed (the watch marks it fixed, e.g. Power Off)")
            cr.position.delete = True
            new = f"(remove '{c.item}' from {order})"
        else:
            if not 0 <= c.move_to < len(order):
                raise ChangeError(f"position outside the list (0..{len(order) - 1}): {order}")
            cr.position.index = c.move_to
            after = [x for x in order if x != c.item]
            after.insert(c.move_to, c.item)
            new = f"{after}"
    else:  # int
        if tname != "TYPE_INTEGER":
            raise ChangeError(f"'{c.item}' is not an integer ({row}/{tname})")
        ip = e.target.integerPicker
        lo, hi = (ip.min, ip.max) if e.target.HasField("integerPicker") and ip.HasField("max") else (None, None)
        log(f"  watch limits: {text_format.MessageToString(ip, as_one_line=True)}")
        if hi is not None and not lo <= c.int <= hi:
            raise ChangeError(f"{c.int} is outside the watch's limits ({lo}–{hi})")
        cr.integerValue.value = c.int
        new = str(c.int)
    return req, new


async def run_change(sess: Session, c: Change, apply: bool, log=print, quiet=lambda m: None) -> Result:
    """Runs ONE change on a connected session. Simulates unless `apply`."""
    kind = c.kind()
    log(f"— {c.path} › {c.item}")
    sid = await navigate(sess, c.path, quiet)
    d, state = await read_screen(sess, sid)
    screen_title = label(d.title)
    e, sort_entry, order = _find_entry(d, state, c, kind)
    req, new = build_request(sid, e, sort_entry, order, c, kind, screen_title, quiet)
    current = f"{order}" if sort_entry is not None else describe(e, state.get(e.id))
    res = Result(c, "simulated", sid, current, new, text_format.MessageToString(req, as_one_line=True))
    log(f"  screen  {c.path}  (screen {sid}, '{screen_title}')")
    log(f"  item    [{req.changeRequest.entryId}] {c.item}")
    log(f"  current {current}")
    log(f"  new     {new}")
    if same_value(kind, current, c, new):
        res.status = "unchanged"
        log("  already set: nothing to send")
        return res
    if not apply:
        log("  simulation: nothing was sent")
        return res

    smart, _ = await sess.change(req, timeout=15)
    if kind in ("select", "remove", "move_to", "text") and screen_title not in ("Options", "Data Fields"):
        sess.nav_cache = None  # screens may have moved or been renamed
    resp = smart.settings_service.changeResponse
    if resp.HasField("unk2"):  # the watch shows an alert (e.g. "Running Threshold Auto Detection Disabled")
        al = resp.unk2
        msg = " / ".join(x for x in (label(al.unk1), label(al.unk2), label(al.unk3)) if x)
        res.alerts.append(msg)
        log(f"  ⚠️ the watch says: {msg}")
    if resp.status != S.ResponseStatus.Value("SUCCESS"):
        res.status = "mismatch"
        res.verified = S.ResponseStatus.Name(resp.status)
        log(f"  ⚠️ the watch did not answer SUCCESS ({res.verified})")
        return res
    ok, res.verified = await _verify(sess, c, kind, sid, e, d, new, quiet)
    res.status = "applied" if ok else "mismatch"
    log(f"  re-read {res.verified}  → {'OK' if ok else 'DIFFERENT ⚠️'}")
    return res


async def _verify(sess, c: Change, kind: str, sid: int, e, d, new: str, quiet) -> tuple[bool, str]:
    if c.check:
        cpath, citem, cval = [x.strip() for x in c.check.split("|")]
        csid = await navigate(sess, cpath, quiet)
        cd, cst = await read_screen(sess, csid)
        ce = [x for x in cd.entry if label(x.title) == citem]
        got = describe(ce[0], cst.get(ce[0].id)) if len(ce) == 1 else f"(item '{citem}' not found or ambiguous)"
        return got == cval, f"{cpath} > {citem} = {got}"
    if kind == "select" and any(x.HasField("sortOptions") for x in d.entry):
        d2, _ = await read_screen(sess, sid)  # selection screen with a sortable list: the pick joins the list
        order2 = [label(se.title) for x in d2.entry if x.HasField("sortOptions") for se in x.sortOptions.entries]
        return c.item in order2, f"{order2}"
    if kind in ("select", "text"):
        parent = c.path.rsplit(">", 1)[0].strip()
        pd, _ = await read_screen(sess, await navigate(sess, parent, quiet))
        names = [label(x.title) for x in pd.entry]
        want = c.text if kind == "text" else c.item
        return want in names, f"{parent}: {names}"
    if kind in ("move_to", "remove"):
        d2, _ = await read_screen(sess, sid)
        order2 = [label(se.title) for x in d2.entry if x.HasField("sortOptions") for se in x.sortOptions.entries]
        if kind == "remove":
            return c.item not in order2, f"{order2}"
        return c.item in order2 and order2.index(c.item) == c.move_to, f"{order2}"
    d2, state2 = await read_screen(sess, sid)
    e2 = next((x for x in d2.entry if x.id == e.id), None)
    if e2 is None:
        return False, "(item gone after writing)"
    after = describe(e2, state2.get(e.id))
    return same_value(kind, after, c, new), after
