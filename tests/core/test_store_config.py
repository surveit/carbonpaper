"""The default storage wiring (app/core/store_config.py): where an entry point
that configures nothing itself ends up reading and writing."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.frames import get_frame_store
from app.core.store_config import configure_default_stores


@pytest.fixture(autouse=True)
def unconfigured_stores(monkeypatch):
    """`configure_default_stores` no-ops unless both globals are None; the suite's fixtures set them."""
    monkeypatch.setattr("app.core.persistence._store", None)
    monkeypatch.setattr("app.core.frames._frame_store", None)


def test_pinning_the_db_path_carries_the_frames_root_with_it(tmp_path, monkeypatch):
    """Otherwise frames resolve against the cwd: a run launched elsewhere silently misses every entry."""
    monkeypatch.setenv("CARBONPAPER_DB_PATH", str(tmp_path / "workspace" / "app.db"))
    monkeypatch.delenv("CARBONPAPER_FRAMES_ROOT", raising=False)

    configure_default_stores()

    assert get_frame_store().root == tmp_path / "workspace" / "frames"


def test_the_frames_root_is_still_separable_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBONPAPER_DB_PATH", str(tmp_path / "workspace" / "app.db"))
    monkeypatch.setenv("CARBONPAPER_FRAMES_ROOT", str(tmp_path / "elsewhere"))

    configure_default_stores()

    assert get_frame_store().root == tmp_path / "elsewhere"


def test_both_defaults_land_under_the_same_relative_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CARBONPAPER_DB_PATH", raising=False)
    monkeypatch.delenv("CARBONPAPER_FRAMES_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    configure_default_stores()

    assert get_frame_store().root == Path("data") / "frames"
    assert (tmp_path / "data").is_dir()
