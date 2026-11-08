import pytest
from fakewatch import RUN, FakeSession

from wristconf.change import Change, ChangeError, navigate, run_change
from wristconf.protocol import S, check_event, request_key

RUN_PATH = "Settings > Activities & Apps > Run"


async def test_navigate_by_names():
    sess = FakeSession()
    assert await navigate(sess, RUN_PATH) == RUN


async def test_navigate_unknown_lists_choices():
    with pytest.raises(ChangeError, match="Available: \\['Run', 'Connect IQ'\\]"):
        await navigate(FakeSession(), "Settings > Activities & Apps > Swim")


async def test_connect_iq_is_blocked():
    with pytest.raises(ChangeError, match="blocked"):
        await navigate(FakeSession(), "Settings > Activities & Apps > Connect IQ")


async def test_simulation_sends_nothing():
    sess = FakeSession()
    r = await run_change(sess, Change(RUN_PATH, "Accent Color", choice="Blue"), apply=False, log=lambda m: None)
    assert (r.status, r.current, r.new) == ("simulated", "Red", "Blue")
    assert sess.sent_changes == []


async def test_apply_writes_and_verifies():
    sess = FakeSession()
    r = await run_change(sess, Change(RUN_PATH, "Accent Color", choice="Blue"), apply=True, log=lambda m: None)
    assert r.status == "applied" and r.verified == "Blue"
    (req,) = sess.sent_changes
    assert (req.changeRequest.screenId, req.changeRequest.entryId, req.changeRequest.option.index) == (RUN, 1, 1)


async def test_already_set_is_skipped():
    sess = FakeSession()
    r = await run_change(sess, Change(RUN_PATH, "Auto Pause", switch=True), apply=True, log=lambda m: None)
    assert r.status == "unchanged" and sess.sent_changes == []


async def test_integer_compares_by_number_and_checks_limits():
    sess = FakeSession()
    r = await run_change(sess, Change(RUN_PATH, "Max. Heart Rate", int=185), apply=True, log=lambda m: None)
    assert r.status == "unchanged"  # "185 bpm" == 185
    with pytest.raises(ChangeError, match="outside the watch's limits"):
        await run_change(sess, Change(RUN_PATH, "Max. Heart Rate", int=250), apply=True, log=lambda m: None)
    assert sess.sent_changes == []


async def test_watch_alert_is_reported():
    sess = FakeSession()
    sess.alert = ("Auto Detection Disabled", "Threshold")
    r = await run_change(sess, Change(RUN_PATH, "Max. Heart Rate", int=190), apply=True, log=lambda m: None)
    assert r.status == "applied"
    assert r.alerts == ["Auto Detection Disabled / Threshold"]


async def test_rename_verifies_in_parent():
    sess = FakeSession()
    r = await run_change(sess, Change(RUN_PATH, "Rename", text="Trail Run"), apply=True, log=lambda m: None)
    assert r.status == "applied"


@pytest.mark.parametrize("change, match", [
    (Change(RUN_PATH, "Restore Defaults", select=True), "destructive"),
    (Change(RUN_PATH, "Accent Color", choice="Purple"), "not a choice"),
    (Change(RUN_PATH, "Accent Color", switch=True), "not a switch"),
    (Change(RUN_PATH, "Auto Pause", choice="Blue"), "not a list"),
    (Change(RUN_PATH, "Rename", text="x" * 21), "longer than 20"),
    (Change(RUN_PATH, "Nope", choice="Blue"), "not found"),
])
async def test_refusals_send_nothing(change, match):
    sess = FakeSession()
    with pytest.raises(ChangeError, match=match):
        await run_change(sess, change, apply=True, log=lambda m: None)
    assert sess.sent_changes == []


def test_change_needs_exactly_one_value():
    with pytest.raises(ChangeError, match="exactly one value"):
        Change(RUN_PATH, "Accent Color").kind()
    with pytest.raises(ChangeError, match="exactly one value"):
        Change(RUN_PATH, "Accent Color", choice="Blue", int=3).kind()


def test_change_from_dict():
    c = Change.from_dict({"path": RUN_PATH, "item": "Auto Pause", "switch": "off"})
    assert c.switch is False
    with pytest.raises(ChangeError, match="unknown keys"):
        Change.from_dict({"path": RUN_PATH, "item": "x", "colour": "Blue"})


def test_writes_need_explicit_permission():
    req = S.SettingsService()
    req.changeRequest.screenId = 1
    with pytest.raises(PermissionError):
        request_key(req, allow_change=False)
    with pytest.raises(PermissionError):
        request_key(S.SettingsService(), allow_change=True)  # empty / unknown request


def test_factory_reset_event_is_never_sent():
    with pytest.raises(PermissionError):
        check_event(2)
    check_event(16)  # TIME_UPDATED is fine
