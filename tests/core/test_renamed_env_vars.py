"""A CARBONPAPER_* variable someone still exports must stop the boot, not be ignored:
booting on default paths against an empty store is indistinguishable from data loss."""
from __future__ import annotations

import os

import pytest

from app.core.store_config import refuse_renamed_env_vars


def test_an_empty_environment_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {})
    refuse_renamed_env_vars()


def test_the_new_names_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {
        "CARBON_PAPER_DB_PATH": "/data/app.db",
        "CARBON_PAPER_PROJECTS_DIR": "/data/projects",
    })
    refuse_renamed_env_vars()


def test_an_old_name_raises_naming_its_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {"CARBONPAPER_DB_PATH": "/data/app.db"})

    with pytest.raises(RuntimeError) as excinfo:
        refuse_renamed_env_vars()

    assert "CARBONPAPER_DB_PATH -> CARBON_PAPER_DB_PATH" in str(excinfo.value)


def test_every_old_name_set_is_reported_not_just_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "environ", {
        "CARBONPAPER_PROJECTS_DIR": "/data/projects",
        "CARBONPAPER_LLM_PARALLEL": "8",
        "PATH": "/usr/bin",
    })

    with pytest.raises(RuntimeError) as excinfo:
        refuse_renamed_env_vars()

    message = str(excinfo.value)
    assert "CARBONPAPER_LLM_PARALLEL -> CARBON_PAPER_LLM_PARALLEL" in message
    assert "CARBONPAPER_PROJECTS_DIR -> CARBON_PAPER_PROJECTS_DIR" in message
    assert "PATH" not in message
