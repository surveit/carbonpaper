"""The default storage wiring (app/core/store_config.py): where an entry point
that configures nothing itself ends up reading and writing."""
from __future__ import annotations

import pytest

import app.core.store_config as store_config
from app.core.frames import get_frame_store
from app.core.paths import CARBON_PAPER_HOME
from app.core.store_config import configure_default_stores, resolve_db_path


@pytest.fixture(autouse=True)
def unconfigured_stores(monkeypatch):
    """`configure_default_stores` no-ops unless both globals are None; the suite's fixtures set them."""
    monkeypatch.setattr("app.core.persistence._store", None)
    monkeypatch.setattr("app.core.frames._frame_store", None)


def test_pinning_the_db_path_carries_the_frames_root_with_it(tmp_path, monkeypatch):
    """Otherwise frames resolve against the cwd: a run launched elsewhere silently misses every entry."""
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(tmp_path / "workspace" / "app.db"))
    monkeypatch.delenv("CARBON_PAPER_FRAMES_ROOT", raising=False)

    configure_default_stores()

    assert get_frame_store().root == tmp_path / "workspace" / "frames"


def test_the_frames_root_is_still_separable_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(tmp_path / "workspace" / "app.db"))
    monkeypatch.setenv("CARBON_PAPER_FRAMES_ROOT", str(tmp_path / "elsewhere"))

    configure_default_stores()

    assert get_frame_store().root == tmp_path / "elsewhere"


def test_both_defaults_land_in_the_machine_global_home(tmp_path, monkeypatch):
    """The cwd must play no part: a run started in any checkout reads the one store."""
    monkeypatch.delenv("CARBON_PAPER_DB_PATH", raising=False)
    monkeypatch.delenv("CARBON_PAPER_FRAMES_ROOT", raising=False)
    monkeypatch.setattr(store_config, "CARBON_PAPER_HOME", tmp_path / "home")
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")

    configure_default_stores()

    assert get_frame_store().root == tmp_path / "home" / "frames"
    assert (tmp_path / "home" / "app.db").is_file()


def test_the_default_db_path_is_the_machine_global_home(monkeypatch):
    monkeypatch.delenv("CARBON_PAPER_DB_PATH", raising=False)

    assert resolve_db_path() == CARBON_PAPER_HOME / "app.db"
