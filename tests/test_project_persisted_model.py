"""Project as a PersistedModel: uniqueness comes from the store, not
examples/<name>/ existing on disk; legacy directories backfill idempotently."""
from __future__ import annotations

import json

import pytest

from app.core.errors import ProjectExistsError
from app.services.project import Project, create_project, list_projects, project_meta


def test_create_project_rejects_a_name_with_an_existing_record(tmp_path):
    create_project("dupe", "doc", source="test", examples_dir=tmp_path)

    with pytest.raises(ProjectExistsError):
        create_project("dupe", "other doc", source="test", examples_dir=tmp_path)


def test_bare_directory_does_not_block_creation(tmp_path):
    """A directory that merely exists (e.g. input files a user staged there by
    hand) is not a name clash — create_project writes the project into it."""
    project_dir = tmp_path / "staged"
    project_dir.mkdir()
    (project_dir / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    name = create_project("staged", "Find the money.", source="test", examples_dir=tmp_path)

    assert name == "staged"
    assert (project_dir / "document.md").read_text(encoding="utf-8") == "Find the money."
    assert (project_dir / "input.csv").is_file()
    assert Project.exists("staged")


def test_existing_document_still_refuses_with_a_distinguishable_message(tmp_path):
    project_dir = tmp_path / "content"
    project_dir.mkdir()
    (project_dir / "document.md").write_text("Pre-existing content.", encoding="utf-8")

    with pytest.raises(ProjectExistsError) as excinfo:
        create_project("content", "New doc.", source="test", examples_dir=tmp_path)

    assert "document.md" in str(excinfo.value)

    # And the two refusal messages differ, so a caller can tell them apart.
    create_project("dupe2", "doc", source="test", examples_dir=tmp_path)
    with pytest.raises(ProjectExistsError) as record_clash:
        create_project("dupe2", "other", source="test", examples_dir=tmp_path)
    assert "document.md" not in str(record_clash.value)


def test_backfill_creates_a_record_from_project_json(tmp_path):
    project_dir = tmp_path / "legacy_with_meta"
    project_dir.mkdir()
    (project_dir / "document.md").write_text("Legacy doc.", encoding="utf-8")
    (project_dir / "project.json").write_text(
        json.dumps({
            "title": "Legacy Title", "model": "sonnet", "source": "old ui",
            "created_at": "2020-01-01T00:00:00",
        }),
        encoding="utf-8",
    )

    names = list_projects(examples_dir=tmp_path)

    assert "legacy_with_meta" in names
    meta = project_meta(project_dir)
    assert meta.title == "Legacy Title"
    assert meta.model == "sonnet"
    assert meta.source == "old ui"
    assert meta.created_at == "2020-01-01T00:00:00"


def test_backfill_creates_a_record_for_a_project_with_no_project_json(tmp_path):
    """A legacy project directory that predates project.json (e.g. carries only
    compiled/ or runs/) still backfills — with an honestly unknown creation
    date, never a fabricated one."""
    project_dir = tmp_path / "legacy_no_meta"
    (project_dir / "compiled").mkdir(parents=True)

    names = list_projects(examples_dir=tmp_path)

    assert "legacy_no_meta" in names
    meta = project_meta(project_dir)
    assert meta.title is None
    assert meta.model is None
    assert meta.source is None
    # The unknown creation date reports as unknown, not as the migration time
    # (PersistedModel.created_at, which the backfill call itself stamps).
    assert meta.created_at is None
    record = Project.load("legacy_no_meta")
    assert record.authored_at is None
    assert record.created_at is not None  # the RECORD was written just now


def test_backfill_is_idempotent(tmp_path):
    project_dir = tmp_path / "legacy_twice"
    project_dir.mkdir()
    (project_dir / "project.json").write_text(
        json.dumps({"title": "T", "model": "sonnet", "source": "s", "created_at": "2021-06-01T00:00:00"}),
        encoding="utf-8",
    )

    list_projects(examples_dir=tmp_path)
    first = Project.load("legacy_twice")

    list_projects(examples_dir=tmp_path)
    second = Project.load("legacy_twice")

    assert first.model_dump() == second.model_dump()


def test_backfill_does_not_adopt_a_directory_with_no_project_artifacts(tmp_path):
    """A bare directory holding only incidental content (no document.md,
    project.json, or any recognized workflow artifact) is not a project and
    must not be silently claimed by the backfill."""
    project_dir = tmp_path / "just_files"
    project_dir.mkdir()
    (project_dir / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    names = list_projects(examples_dir=tmp_path)

    assert "just_files" not in names
    assert not Project.exists("just_files")
