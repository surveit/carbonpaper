"""create_project service: sanitization, on-disk artifacts, loud failure on clash."""
from __future__ import annotations

import pytest

from app.core.errors import ProjectExistsError
from app.services.project import Project, create_project


def test_create_project_writes_document_and_identity_record(projects_root):
    name = create_project("My Probe!", "Find the money.", source="test")
    assert name == "my_probe_"
    pdir = projects_root / name
    assert (pdir / "document.md").read_text(encoding="utf-8") == "Find the money."
    record = Project.load(name)
    assert record.model == "sonnet"
    assert record.source == "test"
    assert record.authored_at  # real timestamp recorded, never fabricated later


def test_create_project_leaves_the_identity_only_in_the_store(projects_root):
    # The record is the one source of truth: no second copy on disk to drift from it.
    create_project("solo", "doc", source="test")
    assert sorted(p.name for p in (projects_root / "solo").iterdir()) == ["document.md"]


def test_create_project_rejects_empty_document(projects_root):
    with pytest.raises(ValueError):
        create_project("x", "   ", source="test")
    assert not (projects_root / "x").exists()


def test_create_project_never_clobbers(projects_root):
    create_project("dupe", "doc", source="test")
    with pytest.raises(ProjectExistsError):
        create_project("dupe", "other doc", source="test")
    assert (projects_root / "dupe" / "document.md").read_text(encoding="utf-8") == "doc"
