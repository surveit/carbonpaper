"""A project is identified by its minted record id. The name is a label: it addresses
nothing, so nothing breaks when it changes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import workspace
from app.services.project import create_project, list_projects, project_meta
from app.services.project_record import (
    Project,
    find_project_by_name,
    mint_project_id,
    resolve_project_id,
)


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path) -> Path:
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    workspace.set_projects_dir(examples_dir)
    return examples_dir


def test_the_record_id_is_not_the_name(workspace_root: Path) -> None:
    create_project("my_investigation", "prose", source="test")

    record = find_project_by_name("my_investigation")

    assert record is not None
    assert record.name == "my_investigation"
    assert record.id != "my_investigation"


def test_two_projects_get_two_ids(workspace_root: Path) -> None:
    create_project("first", "prose", source="test")
    create_project("second", "prose", source="test")

    ids = {record.id for record in Project.list()}

    assert len(ids) == 2


def test_renaming_the_label_keeps_the_record(workspace_root: Path) -> None:
    """The point of the surrogate id: the record survives its name changing."""
    create_project("old_name", "prose", source="test")
    record = find_project_by_name("old_name")
    assert record is not None
    original_id = record.id

    record.name = "new_name"
    record.save()

    assert find_project_by_name("old_name") is None
    renamed = find_project_by_name("new_name")
    assert renamed is not None and renamed.id == original_id


def test_resolve_returns_none_for_a_directory_with_no_record(workspace_root: Path) -> None:
    """A hand-copied directory is a project the workspace holds and the store does not."""
    (workspace_root / "copied_in").mkdir()

    assert resolve_project_id("copied_in") is None


def test_meta_and_listing_still_speak_in_names(workspace_root: Path) -> None:
    create_project("my_investigation", "prose", model="sonnet", source="test")

    assert list_projects() == ["my_investigation"]
    meta = project_meta(workspace_root / "my_investigation")
    assert meta.name == "my_investigation"
    assert meta.model == "sonnet"


def test_a_record_id_collision_is_not_possible_by_name(workspace_root: Path) -> None:
    """Two records may carry one name in the store — only the directory forbids it on disk."""
    Project(id=mint_project_id(), name="twin").save()
    Project(id=mint_project_id(), name="twin").save()

    assert len([r for r in Project.list() if r.name == "twin"]) == 2
