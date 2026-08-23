"""create_project service: what it puts on disk, and that a repeated name is not a clash."""
from __future__ import annotations


import pytest

from app.services.project import create_project
from app.services.methodology import read_methodology
from app.models.records.project import Project


def test_create_project_stores_document_and_meta(projects_root):
    project_id = create_project("My Probe!", "Find the money.", source="test").id

    assert read_methodology(project_id) == "Find the money."
    record = Project.load(project_id)
    assert record.name == "my_probe_"  # sanitized label, not the id
    assert record.model == "sonnet"
    assert record.source == "test"
    assert record.authored_at  # real timestamp recorded, never fabricated later


def test_create_project_rejects_empty_document(projects_root):
    with pytest.raises(ValueError):
        create_project("x", "   ", source="test")
    assert list(projects_root.iterdir()) == []


def test_a_repeated_name_makes_a_second_project_and_clobbers_nothing(projects_root):
    first = create_project("dupe", "doc", source="test").id

    second = create_project("dupe", "other doc", source="test").id

    assert first != second
    assert read_methodology(first) == "doc"
    assert read_methodology(second) == "other doc"
