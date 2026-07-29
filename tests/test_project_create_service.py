"""create_project service: sanitization, on-disk artifacts, loud failure on clash."""
from __future__ import annotations

import json

import pytest

from app.core.errors import ProjectExistsError
from app.services.project import create_project


def test_create_project_writes_document_and_meta(projects_root):
    name = create_project("My Probe!", "Find the money.", source="test")
    assert name == "my_probe_"
    pdir = projects_root / name
    assert (pdir / "document.md").read_text(encoding="utf-8") == "Find the money."
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    assert meta["model"] == "sonnet"
    assert meta["source"] == "test"
    assert meta["created_at"]  # real timestamp recorded, never fabricated later


def test_create_project_rejects_empty_document(projects_root):
    with pytest.raises(ValueError):
        create_project("x", "   ", source="test")
    assert not (projects_root / "x").exists()


def test_create_project_never_clobbers(projects_root):
    create_project("dupe", "doc", source="test")
    with pytest.raises(ProjectExistsError):
        create_project("dupe", "other doc", source="test")
    assert (projects_root / "dupe" / "document.md").read_text(encoding="utf-8") == "doc"
