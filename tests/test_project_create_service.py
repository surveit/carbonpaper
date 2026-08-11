"""create_project service: what it puts on disk, and that a repeated name is not a clash."""
from __future__ import annotations

import json

import pytest

from app.services.project import create_project


def test_create_project_writes_document_and_meta(projects_root):
    project_id = create_project("My Probe!", "Find the money.", source="test")

    pdir = projects_root / project_id
    assert (pdir / "document.md").read_text(encoding="utf-8") == "Find the money."
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    assert meta["name"] == "my_probe_"  # sanitized label, not the id
    assert meta["model"] == "sonnet"
    assert meta["source"] == "test"
    assert meta["created_at"]  # real timestamp recorded, never fabricated later


def test_create_project_rejects_empty_document(projects_root):
    with pytest.raises(ValueError):
        create_project("x", "   ", source="test")
    assert list(projects_root.iterdir()) == []


def test_a_repeated_name_makes_a_second_project_and_clobbers_nothing(projects_root):
    first = create_project("dupe", "doc", source="test")

    second = create_project("dupe", "other doc", source="test")

    assert first != second
    assert (projects_root / first / "document.md").read_text(encoding="utf-8") == "doc"
    assert (projects_root / second / "document.md").read_text(
        encoding="utf-8") == "other doc"
