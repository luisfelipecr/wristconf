"""A watch in memory: a tiny settings tree served over the same interface as protocol.Session.

Lets the tests exercise navigation, snapshots, simulations and writes without Bluetooth.
"""

from __future__ import annotations

from wristconf.protocol import ROOT_SCREEN_ID, S, Smart, request_key

RUN, SYSTEM, ABOUT, ACTIVITIES, CIQ = 100, 200, 210, 90, 2166


def _entry(d, id_, title, row, target=None, subscreen=None):
    e = d.entry.add()
    e.id, e.rowType = id_, S.RowType.Value(row)
    e.title.text = title
    if target is not None:
        e.target.type = S.Target.Type.Value(target)
    if subscreen is not None:
        e.target.subscreen = subscreen
    return e


def build_tree():
    """{screen id: ScreenDefinition}, {screen id: {entry id: EntryState}}."""
    defs, states = {}, {}

    def screen(sid, title):
        d = S.ScreenDefinition()
        d.screenId = sid
        d.title.text = title
        defs[sid], states[sid] = d, {}
        return d

    root = screen(ROOT_SCREEN_ID, "Settings")
    _entry(root, 1, "Activities & Apps", "ROW_SINGLE_LINE", "TYPE_SUBSCREEN", ACTIVITIES)
    _entry(root, 2, "System", "ROW_SINGLE_LINE", "TYPE_SUBSCREEN", SYSTEM)

    acts = screen(ACTIVITIES, "Activities & Apps")
    _entry(acts, 1, "Run", "ROW_SINGLE_LINE", "TYPE_SUBSCREEN", RUN)
    _entry(acts, 2, "Connect IQ", "ROW_SINGLE_LINE", "TYPE_SUBSCREEN", CIQ)

    run = screen(RUN, "Run")
    e = _entry(run, 1, "Accent Color", "ROW_DOUBLE_LINE", "TYPE_LIST")
    for name in ("Red", "Blue", "Green"):
        e.target.options.option.add().title.text = name
    _entry(run, 2, "Auto Pause", "ROW_SWITCH")
    e = _entry(run, 3, "Max. Heart Rate", "ROW_DOUBLE_LINE", "TYPE_INTEGER")
    e.target.integerPicker.min, e.target.integerPicker.max, e.target.integerPicker.step = 100, 220, 1
    _entry(run, 4, "Restore Defaults", "ROW_ACTION")
    _entry(run, 5, "Rename", "ROW_TEXT").textOption.limits.maxLength = 20
    st = states[RUN]
    st[1] = S.EntryState(id=1)
    st[1].summary.valueList.index = 0
    st[2] = S.EntryState(id=2)
    st[2].switch.enabled = True
    st[3] = S.EntryState(id=3)
    st[3].summary.valueInteger.value = 185
    st[3].summary.valueInteger.formatted.text = "185 bpm"

    system = screen(SYSTEM, "System")
    _entry(system, 1, "About", "ROW_SINGLE_LINE", "TYPE_SUBSCREEN", ABOUT)
    about = screen(ABOUT, "About")
    for i, text in enumerate(["fenix 8 - 47mm", "1234567890", "Software Version", "23.40", "[DFU-abc123]"], 1):
        _entry(about, i, text, "ROW_NOTICE")
    return defs, states


class FakeSession:
    """Answers definition/state requests from the tree; applies ChangeRequests to the state."""

    def __init__(self):
        self.defs, self.states = build_tree()
        self.nav_cache = None
        self.sent_changes: list[S.SettingsService] = []
        self.requested: list[int] = []
        self.alert: tuple[str, str] | None = None

    async def request(self, req, timeout=15.0):
        request_key(req, allow_change=False)  # same guard as the real watch connection
        smart = Smart()
        ss = smart.settings_service
        if req.HasField("definitionRequest"):
            sid = req.definitionRequest.screenId
            self.requested.append(sid)
            if sid not in self.defs:
                raise TimeoutError
            ss.definitionResponse.status = 0
            ss.definitionResponse.definition.CopyFrom(self.defs[sid])
        else:
            sid = req.stateRequest.screenId
            ss.stateResponse.state.screenId = sid
            for st in self.states.get(sid, {}).values():
                ss.stateResponse.state.state.add().CopyFrom(st)
        return smart, smart.SerializeToString()

    async def change(self, req, timeout=15.0):
        request_key(req, allow_change=True)
        self.sent_changes.append(req)
        cr = req.changeRequest
        st = self.states[cr.screenId].setdefault(cr.entryId, S.EntryState(id=cr.entryId))
        if cr.HasField("option"):
            st.summary.valueList.index = cr.option.index
        elif cr.HasField("switch"):
            st.switch.enabled = cr.switch.value
        elif cr.HasField("integerValue"):
            st.summary.valueInteger.value = cr.integerValue.value
            st.summary.valueInteger.formatted.text = f"{cr.integerValue.value} bpm"
        elif cr.HasField("text"):  # "Rename" on the Run screen renames the activity, as on the watch
            for e in self.defs[ACTIVITIES].entry:
                if e.target.subscreen == cr.screenId:
                    e.title.text = cr.text.value
            self.defs[cr.screenId].title.text = cr.text.value
        smart = Smart()
        resp = smart.settings_service.changeResponse
        resp.status = S.ResponseStatus.Value("SUCCESS")
        resp.state.screenId = cr.screenId
        if self.alert:
            resp.unk2.unk1.text, resp.unk2.unk2.text = self.alert
        return smart, smart.SerializeToString()
