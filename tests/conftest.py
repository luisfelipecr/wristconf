import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # fakewatch


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never touch the real config or data folders."""
    monkeypatch.setenv("WRISTCONF_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("WRISTCONF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("WRISTCONF_DEVICE", raising=False)
