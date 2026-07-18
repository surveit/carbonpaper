"""create_project service: sanitization, on-disk artifacts, loud failure on clash."""
from __future__ import annotations

import json

import pytest

from app.core.errors import ProjectExistsError
from app.services.project import create_project


def test_create_project_writes_document_and_meta(tmp_path):
    name = create_project("My Probe!", "Find the money.", source="test", examples_dir=tmp_path)
    assert name == "my_probe_"
    pdir = tmp_path / name
    assert (pdir / "document.md").read_text(encoding="utf-8") == "Find the money."
    meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    assert meta["model"] == "sonnet"
    assert meta["source"] == "test"
    assert meta["created_at"]  # real timestamp recorded, never fabricated later


def test_create_project_rejects_empty_document(tmp_path):
    with pytest.raises(ValueError):
        create_project("x", "   ", source="test", examples_dir=tmp_path)
    assert not (tmp_path / "x").exists()


def test_create_project_never_clobbers(tmp_path):
    create_project("dupe", "doc", source="test", examples_dir=tmp_path)
    with pytest.raises(ProjectExistsError):
        create_project("dupe", "other doc", source="test", examples_dir=tmp_path)
    assert (tmp_path / "dupe" / "document.md").read_text(encoding="utf-8") == "doc"
