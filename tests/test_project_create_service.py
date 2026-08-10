"""create_project service: sanitization, on-disk artifacts, loud failure on clash."""
from __future__ import annotations


import pytest

from app.core.errors import ProjectExistsError
from app.services.project import create_project
from app.services.methodology import read_methodology
from app.services.project import Project


def test_create_project_stores_document_and_meta(projects_root):
    name = create_project("My Probe!", "Find the money.", source="test")
    assert name == "my_probe_"
    assert read_methodology(name) == "Find the money."
    record = Project.load(name)
    assert record.model == "sonnet"
    assert record.source == "test"
    assert record.authored_at  # real timestamp recorded, never fabricated later


def test_create_project_rejects_empty_document(projects_root):
    with pytest.raises(ValueError):
        create_project("x", "   ", source="test")
    assert not (projects_root / "x").exists()


def test_create_project_never_clobbers(projects_root):
    create_project("dupe", "doc", source="test")
    with pytest.raises(ProjectExistsError):
        create_project("dupe", "other doc", source="test")
    assert read_methodology("dupe") == "doc"
