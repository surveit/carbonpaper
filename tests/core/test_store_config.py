"""The default storage wiring (app/core/store_config.py): where an entry point
that configures nothing itself ends up reading and writing."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.frames import get_frame_store
from app.core.store_config import configure_default_stores


@pytest.fixture(autouse=True)
def unconfigured_stores(monkeypatch):
    """The suite's autouse fixtures configure both stores, and
    `configure_default_stores` is guarded by exactly that — so nothing here is
    observable until both globals are back to their process-start state."""
    monkeypatch.setattr("app.core.persistence._store", None)
    monkeypatch.setattr("app.core.frames._frame_store", None)


def test_pinning_the_db_path_carries_the_frames_root_with_it(tmp_path, monkeypatch):
    """A cache entry spans both stores, so a deployment that pins CARBONPAPER_DB_PATH and
    says nothing about frames must not leave the frame payloads resolving
    against the working directory: a run launched from elsewhere would then miss
    every frame entry silently and re-pin duplicates under the new cwd."""
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
    """With nothing set at all, the pair is `data/app.db` + `data/frames`,
    relative to the working directory — the shape the repo runs with."""
    monkeypatch.delenv("CARBONPAPER_DB_PATH", raising=False)
    monkeypatch.delenv("CARBONPAPER_FRAMES_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    configure_default_stores()

    assert get_frame_store().root == Path("data") / "frames"
    assert (tmp_path / "data").is_dir()
