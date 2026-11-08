import json

from fakewatch import ABOUT, CIQ, RUN, FakeSession

from wristconf import catalog as C
from wristconf.diff import compare, format_diff
from wristconf.snapshot import take_quick, walk


async def _snapshot(tmp_path, name="snap", sess=None):
    out = tmp_path / name
    out.mkdir()
    sess = sess or FakeSession()
    assert await walk(sess, out, log=lambda m: None) is True
    return out, sess


async def test_walk_reads_the_tree_and_skips_connect_iq(tmp_path):
    out, sess = await _snapshot(tmp_path)
    assert CIQ not in sess.requested
    assert (out / "screens" / f"{RUN}.def.bin").exists()
    stats = json.loads((out / "state.json").read_text())["stats"]
    assert stats["screens"] == 5 and stats["skipped"][0]["screen"] == CIQ  # root, apps, run, system, about


async def test_catalog_keeps_values_until_sanitized(tmp_path):
    out, _ = await _snapshot(tmp_path)
    c = C.build(out)
    assert (c["model"], c["firmware"]) == ("fenix 8 - 47mm", "23.40")
    run = c["screens"][str(RUN)]
    assert run["path"] == "Settings > Activities & Apps > Run"
    values = {e["label"]: e["value"] for e in run["entries"]}
    assert values["Accent Color"] == "Red" and values["Max. Heart Rate"] == "185 bpm"

    s = C.sanitize(c)
    blob = json.dumps(s)
    assert '"value"' not in blob and "185" not in blob and "integer" not in blob
    about = [e["label"] for e in s["screens"][str(ABOUT)]["entries"]]
    assert "1234567890" not in about and "<serial number>" in about
    assert "[DFU-<redacted>]" in about
    assert s["sanitized"] is True and c["sanitized"] is False  # the original is untouched


async def test_review_list_flags_names_not_in_reference(tmp_path):
    out, sess = await _snapshot(tmp_path)
    reference = C.sanitize(C.build(out))
    sess2 = FakeSession()
    sess2.defs[90].entry[0].title.text = "Grandma's Hike"
    out2, _ = await _snapshot(tmp_path, "snap2", sess2)
    assert C.review_list(C.sanitize(C.build(out2)), [reference]) == ["Grandma's Hike"]


async def test_quick_snapshot_and_diff(tmp_path):
    base, _ = await _snapshot(tmp_path)
    sess = FakeSession()
    sess.states[RUN][1].summary.valueList.index = 2  # Accent Color: Red -> Green
    out = tmp_path / "quick"
    out.mkdir()
    stats = await take_quick(sess, base, out, log=lambda m: None)
    assert stats["changed"] == ["Settings > Activities & Apps > Run"] and stats["new"] == []
    d = compare(C.build(base), C.build(out))
    assert d.values == [("Settings > Activities & Apps > Run", "Accent Color", ("Red", None, ()), ("Green", None, ()))]
    assert "value 'Red' → 'Green'" in format_diff(d)


async def test_render_and_find(tmp_path):
    out, _ = await _snapshot(tmp_path)
    s = C.sanitize(C.build(out))
    md = C.render_markdown(s)
    assert "Settings > Activities & Apps > Run" in md and "Red · Blue · Green" in md and "| value |" not in md
    hits = C.find(s, "blue")
    assert [(h["path"], h["item"]) for h in hits] == [("Settings > Activities & Apps > Run", "Accent Color")]


def test_bundled_catalogs_are_sanitized():
    paths = C.bundled()
    assert paths, "the package ships at least one catalog"
    for p in paths:
        c = C.load(p)
        assert c["sanitized"] is True and c["model"] and c["firmware"]
        assert p.parent.name == C.slug(c["model"]) and p.name == f"fw-{c['firmware']}.json"
        for sc in c["screens"].values():
            for e in sc["entries"]:
                assert not set(e) & {"value", "state_flag", "sort_entries"}, (p.name, sc["path"], e["label"])
                assert not set(e.get("options", {})) & {"integer", "duration_s", "float"}
                assert not C._SERIAL.match(e["label"])
                if e["row"] == "ROW_NOTICE" and sc["title"] != "About":
                    assert not C._MEASURE.match(e["label"]), (p.name, sc["path"], e["label"])


def test_sanitize_hides_values_shown_as_notices():
    cat = {"screens": {"1": {"title": "Fitness Age", "path": "Settings > User Profile > Fitness Age", "status": 0,
                             "menu": [],
                             "entries": [{"id": 1, "label": "41.0", "row": "ROW_NOTICE", "target": None},
                                         {"id": 2, "label": "11.6 GB", "row": "ROW_NOTICE", "target": None},
                                         {"id": 3, "label": "Zone 1", "row": "ROW_DOUBLE_LINE", "target": None}]}}}
    assert [e["label"] for e in C.sanitize(cat)["screens"]["1"]["entries"]] == ["<value>", "<value>", "Zone 1"]
