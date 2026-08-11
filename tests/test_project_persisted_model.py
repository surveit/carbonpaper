"""Project as a PersistedModel: uniqueness comes from the store, not from
<projects root>/<name>/ existing on disk."""
from __future__ import annotations

import pytest

from app.core.errors import ProjectExistsError
from app.services.project import create_project
from app.services.project_record import find_projects_by_name


def test_create_project_rejects_a_name_with_an_existing_record(tmp_path):
    create_project("dupe", "doc", source="test")

    with pytest.raises(ProjectExistsError):
        create_project("dupe", "other doc", source="test")


def test_bare_directory_does_not_block_creation(projects_root):
    project_dir = projects_root / "staged"
    project_dir.mkdir()
    (project_dir / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    name = create_project("staged", "Find the money.", source="test")

    assert name == "staged"
    assert (project_dir / "document.md").read_text(encoding="utf-8") == "Find the money."
    assert (project_dir / "input.csv").is_file()
    assert find_projects_by_name("staged")


def test_existing_document_still_refuses_with_a_distinguishable_message(projects_root):
    project_dir = projects_root / "content"
    project_dir.mkdir()
    (project_dir / "document.md").write_text("Pre-existing content.", encoding="utf-8")

    with pytest.raises(ProjectExistsError) as excinfo:
        create_project("content", "New doc.", source="test")

    assert "document.md" in str(excinfo.value)

    # And the two refusal messages differ, so a caller can tell them apart.
    create_project("dupe2", "doc", source="test")
    with pytest.raises(ProjectExistsError) as record_clash:
        create_project("dupe2", "other", source="test")
    assert "document.md" not in str(record_clash.value)

