"""Comparing two catalogs BY PATH (screen ids change between firmware versions).

Use it after a firmware update, or before/after changing settings.
- Screens match by `path`. When several screens share a path (several "Edit Screen"), they match by
  the most similar set of items.
- Items match by (label, occurrence) inside a screen, never by id.
- "Value" covers the current value, the state flag and the order of sortable lists.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field


def _key_struct(e: dict):
    return (e["row"], e["target"], json.dumps(e.get("options", {}), sort_keys=True))


def _key_value(e: dict):
    return (e.get("value"), e.get("state_flag"), tuple(e.get("sort_entries") or ()))


def _entry_map(screen: dict) -> dict:
    seen, out = Counter(), {}
    for e in screen["entries"]:
        lab = e["label"]
        out[(lab, seen[lab])] = e
        seen[lab] += 1
    return out


def _similarity(a: dict, b: dict) -> float:
    la, lb = set(_entry_map(a)), set(_entry_map(b))
    return len(la & lb) / max(1, len(la | lb))


def pair_screens(old: dict, new: dict):
    """[(path, old screen or None, new screen or None)]."""
    by_old, by_new = defaultdict(list), defaultdict(list)
    for s in old.values():
        by_old[s["path"]].append(s)
    for s in new.values():
        by_new[s["path"]].append(s)
    pairs = []
    for path in sorted(set(by_old) | set(by_new)):
        a, b = list(by_old.get(path, [])), list(by_new.get(path, []))
        while a and b:
            i, j = max(((i, j) for i in range(len(a)) for j in range(len(b))),
                       key=lambda ij: _similarity(a[ij[0]], b[ij[1]]))
            pairs.append((path, a.pop(i), b.pop(j)))
        pairs += [(path, x, None) for x in a] + [(path, None, y) for y in b]
    return pairs


@dataclass
class Diff:
    old_screens: int
    new_screens: int
    matched: int
    screens_removed: list[str] = field(default_factory=list)
    screens_added: list[str] = field(default_factory=list)
    structure: list[tuple[str, str, str]] = field(default_factory=list)  # (path, item, what)
    values: list[tuple[str, str, tuple, tuple]] = field(default_factory=list)  # (path, item, old, new)

    @property
    def empty(self) -> bool:
        return not (self.screens_removed or self.screens_added or self.structure or self.values)


def compare(old_cat: dict, new_cat: dict) -> Diff:
    old, new = old_cat["screens"], new_cat["screens"]
    pairs = pair_screens(old, new)
    d = Diff(len(old), len(new), sum(1 for p in pairs if p[1] and p[2]))
    d.screens_removed = [p for p, _, n in pairs if n is None]
    d.screens_added = [p for p, o, _ in pairs if o is None]
    for path, x, y in pairs:
        if not (x and y):
            continue
        ea, eb = _entry_map(x), _entry_map(y)
        for k in sorted(set(ea) | set(eb)):
            p, q = ea.get(k), eb.get(k)
            name = k[0] + (f" #{k[1] + 1}" if k[1] else "")
            if p is None or q is None:
                d.structure.append((path, name, "removed" if q is None else "added"))
            elif _key_struct(p) != _key_struct(q):
                d.structure.append((path, name, "type or choices changed"))
            elif _key_value(p) != _key_value(q):
                d.values.append((path, name, _key_value(p), _key_value(q)))
    return d


def format_diff(d: Diff, max_lines: int = 200) -> str:
    L = [f"screens: old {d.old_screens} · new {d.new_screens} · matched by path {d.matched}"]
    if d.screens_removed:
        L += ["", f"- screens only in the old one ({len(d.screens_removed)}):"]
        L += [f"   {p}" for p in d.screens_removed[:max_lines]]
    if d.screens_added:
        L += ["", f"+ screens only in the new one ({len(d.screens_added)}):"]
        L += [f"   {p}" for p in d.screens_added[:max_lines]]
    L += ["", f"items added, removed or with a different structure: {len(d.structure)}"]
    L += [f"   {path} · {name!r}: {what}" for path, name, what in d.structure[:max_lines]]
    L += ["", f"items with a different value, state or order: {len(d.values)}"]
    for path, name, p, q in d.values[:max_lines]:
        parts = []
        if p[0] != q[0]:
            parts.append(f"value {p[0]!r} → {q[0]!r}")
        if p[1] != q[1]:
            parts.append(f"state {p[1]} → {q[1]}")
        if p[2] != q[2]:
            parts.append(f"order {list(p[2])} → {list(q[2])}")
        L.append(f"   {path} · {name!r}: {'; '.join(parts)}")
    areas = Counter(r[0].split(" > ")[1] if " > " in r[0] else r[0] for r in d.structure + d.values)
    if areas:
        L += ["", "differences by area: " + ", ".join(f"{a} {n}" for a, n in areas.most_common(10))]
    return "\n".join(L) + "\n"
