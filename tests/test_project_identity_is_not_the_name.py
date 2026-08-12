"""A project IS its id, which is also the name of its directory. `name` is a display
label: it addresses nothing, it may change, and two projects may share one.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.services import workspace
from app.services.project import create_project, list_projects, project_meta
from app.services.project import (
    Project,
    describe_project,
    find_projects_by_name,
)


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path) -> Path:
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    workspace.set_projects_dir(examples_dir)
    return examples_dir


def test_create_returns_an_id_that_is_not_the_name(workspace_root: Path) -> None:
    project_id = create_project("my_investigation", "prose", source="test").id

    assert project_id != "my_investigation"
    assert Project.load(project_id).name == "my_investigation"


def test_the_directory_is_named_by_the_id(workspace_root: Path) -> None:
    project_id = create_project("my_investigation", "prose", source="test").id

    assert (workspace_root / project_id / "document.md").is_file()
    assert not (workspace_root / "my_investigation").exists()


def test_two_projects_may_share_a_name(workspace_root: Path) -> None:
    """The point of the id: a second investigation of the same subject is not a clash."""
    first = create_project("my_investigation", "first prose", source="test").id
    second = create_project("my_investigation", "second prose", source="test").id

    assert first != second
    assert {r.id for r in find_projects_by_name("my_investigation")} == {first, second}
    assert (workspace_root / first / "document.md").read_text(
        encoding="utf-8") == "first prose"
    assert (workspace_root / second / "document.md").read_text(
        encoding="utf-8") == "second prose"


def test_a_name_is_reusable_after_its_project_is_deleted(workspace_root: Path) -> None:
    """The tombstone bug, gone by construction: nothing is ever checked against a name."""
    first = create_project("my_investigation", "prose", source="test").id
    shutil.rmtree(workspace_root / first)

    second = create_project("my_investigation", "prose", source="test").id

    assert second != first


def test_renaming_the_label_moves_nothing(workspace_root: Path) -> None:
    project_id = create_project("old_name", "prose", source="test").id
    record = Project.load(project_id)

    record.name = "new_name"
    record.save()

    assert Project.load(project_id).name == "new_name"
    assert (workspace_root / project_id / "document.md").is_file()
    assert find_projects_by_name("old_name") == []


def test_listing_and_meta_agree_on_which_is_which(workspace_root: Path) -> None:
    project_id = create_project("my_investigation", "prose", model="sonnet", source="test").id

    assert list_projects() == [project_id]
    meta = project_meta(workspace_root / project_id)
    assert meta.name == "my_investigation"
    assert meta.model == "sonnet"


def test_a_directory_with_no_record_is_its_own_id(workspace_root: Path) -> None:
    """A project created before ids were minted, or copied in by hand, still resolves."""
    (workspace_root / "copied_in").mkdir()

    assert describe_project("copied_in") == "copied_in"
    assert project_meta(workspace_root / "copied_in").name == "copied_in"


def test_a_record_from_before_labels_existed_still_loads(workspace_root: Path) -> None:
    """No migration: `name` is optional, so a record written without one is valid as it stands."""
    from app.core.persistence import get_store

    get_store().write("project", "venezuela_lda_lobbying", {
        "id": "venezuela_lda_lobbying",
        "created_at": "2026-07-29T13:02:08",
        "updated_at": "2026-07-29T13:02:08",
        "model": "sonnet",
        "source": "mcp",
    })

    record = Project.load("venezuela_lda_lobbying")
    assert record.name is None
    assert record.label() == "venezuela_lda_lobbying"
    assert find_projects_by_name("venezuela_lda_lobbying") == [record]
    assert describe_project("venezuela_lda_lobbying") == "venezuela_lda_lobbying"


def test_the_shown_name_is_the_title_where_one_is_authored(workspace_root: Path) -> None:
    """The trail, the sidebar and the home card all read this one resolution."""
    project_id = create_project("dsa_evidence_capture", "prose", source="test").id
    record = Project.load(project_id)
    record.title = "DSA takedown evidence capture"
    record.save()

    assert describe_project(project_id) == "DSA takedown evidence capture"
    assert project_meta(workspace_root / project_id).display_name == (
        "DSA takedown evidence capture")
    # The slug survives it: a bundle exports under that, and it is what a name lookup takes.
    assert project_meta(workspace_root / project_id).name == "dsa_evidence_capture"
    assert find_projects_by_name("dsa_evidence_capture") == [Project.load(project_id)]


def test_a_project_json_with_no_record_is_still_read_by_name(workspace_root: Path) -> None:
    """A project copied onto disk has a name in project.json and nothing in the store."""
    pdir = workspace_root / "congress_roster_diff"
    pdir.mkdir()
    (pdir / "project.json").write_text(json.dumps({
        "name": "congress_roster_diff",
        "title": "Congress roster — diffing two CSV snapshots",
        "created_at": "2026-08-06T11:30:00",
        "model": None,
        "source": "manual",
    }), encoding="utf-8")

    assert describe_project("congress_roster_diff") == (
        "Congress roster — diffing two CSV snapshots")
    meta = project_meta(pdir)
    assert meta.display_name == "Congress roster — diffing two CSV snapshots"
    assert meta.created_at == "2026-08-06T11:30:00"
    assert meta.source == "manual"
