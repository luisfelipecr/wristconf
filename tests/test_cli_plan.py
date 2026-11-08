import pytest

from wristconf import cli, device
from wristconf.change import ChangeError
from wristconf.plan import load


def test_preset_loads(tmp_path):
    f = tmp_path / "p.toml"
    f.write_text('name = "Test"\n[[change]]\npath = "Settings > A"\nitem = "B"\nchoice = "C"\n'
                 '[[change]]\npath = "Settings > A"\nitem = "D"\nswitch = false\n')
    p = load(f)
    assert p.name == "Test" and [c.kind() for c in p.changes] == ["choice", "switch"]


@pytest.mark.parametrize("body, match", [
    ('name = "x"\n', "no \\[\\[change\\]\\]"),
    ('[[change]]\npath = "A"\nitem = "B"\n', "exactly one value"),
    ('[[change]]\npath = "A"\nitem = "B"\ncolour = "red"\n', "unknown keys"),
    ('oops = 1\n[[change]]\npath = "A"\nitem = "B"\nint = 1\n', "unknown top-level"),
    ('[[change]\n', "invalid TOML"),
])
def test_preset_errors(tmp_path, body, match):
    f = tmp_path / "p.toml"
    f.write_text(body)
    with pytest.raises(ChangeError, match=match):
        load(f)


def test_examples_pass_the_offline_check():
    from pathlib import Path

    from wristconf import catalog
    from wristconf.plan import check

    newest = catalog.load(catalog.bundled()[-1])
    examples = sorted((Path(__file__).parent.parent / "examples").glob("*.toml"))
    assert examples
    for f in examples:
        assert check(load(f), newest) == [], f.name


def test_offline_check_catches_typos(tmp_path):
    from wristconf import catalog
    from wristconf.plan import check

    f = tmp_path / "p.toml"
    f.write_text('[[change]]\npath = "Settings > Activities & Apps > Activities > Run"\nitem = "Accent Color"\n'
                 'choice = "Chartreuse"\n'
                 '[[change]]\npath = "Settings > Activities & Apps > Activities > Runn"\nitem = "X"\nint = 1\n'
                 '[[change]]\npath = "Settings > Activities & Apps > Activities > Run"\nitem = "Accent Color"\n'
                 'switch = true\n')
    problems = check(load(f), catalog.load(catalog.bundled()[-1]))
    assert len(problems) == 3
    assert "'Chartreuse' is not a choice" in problems[0]
    assert "no screen with this path" in problems[1]
    assert "does not take switch" in problems[2]


def test_address_detection():
    assert device.looks_like_address("12:34:56:78:9A:BC")
    assert device.looks_like_address("1C5A4C2E-0B7F-4E1A-9C3D-2B6E7F8A9B0C")
    assert not device.looks_like_address("fenix 8")


def test_saved_device(tmp_path):
    assert device.resolve_device(None) is None
    device.save_device("AA:BB:CC:DD:EE:FF")
    assert device.resolve_device(None) == "AA:BB:CC:DD:EE:FF"
    assert device.resolve_device("other") == "other"


def test_find_uses_bundled_catalog(capsys):
    assert cli.main(["find", "accent color"]) == 0
    out = capsys.readouterr().out
    assert "fenix 8 - 47mm" in out and "Accent Color" in out


def test_catalog_list(capsys):
    assert cli.main(["catalog", "list"]) == 0
    assert "firmware 23.40" in capsys.readouterr().out


def test_set_refuses_to_write_without_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="--yes"):
        cli._confirm("Write?", assume_yes=False)
    assert cli._confirm("Write?", assume_yes=True)


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("scan", "pair", "snapshot", "diff", "show", "find", "set", "plan", "apply", "catalog"):
        assert cmd in out
    assert "_snapshot-worker" not in out


async def test_plan_offline_against_a_snapshot(tmp_path, capsys):
    import asyncio

    from fakewatch import FakeSession

    from wristconf.snapshot import walk

    snap = tmp_path / "snap"
    snap.mkdir()
    await walk(FakeSession(), snap, log=lambda m: None)
    f = tmp_path / "p.toml"
    f.write_text('[[change]]\npath = "Settings > Activities & Apps > Run"\nitem = "Accent Color"\nchoice = "Blue"\n'
                 '[[change]]\npath = "Settings > Activities & Apps > Run"\nitem = "Auto Pause"\nswitch = "on"\n')
    rc = await asyncio.to_thread(cli.main, ["plan", str(f), "--offline", str(snap)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "plan: 1 to change, 1 already set" in out and "Accent Color: Red → Blue" in out


def test_info_output_hides_identifiers():
    assert "1234567890" not in cli.mask_identifiers("1234567890")
    assert cli.mask_identifiers("[DFU-abc123]") == "[DFU-<hidden>]"
    assert cli.mask_identifiers("GPS: 11.02") == "GPS: 11.02"
    assert cli.mask_identifiers("23.40") == "23.40"
