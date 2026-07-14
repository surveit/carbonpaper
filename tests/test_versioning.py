"""Version lifecycle: born unpublished, published by an explicit idempotent act;
versions predating the flag read as published (they were human-created)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import versioning

_STAGE = {
    "id": "load",
    "name": "Load rows",
    "type": "input_data",
    "connector": {"kind": "computed_static"},
}


def _seed_working_copy(project_dir: Path) -> None:
    compiled = project_dir / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_STAGE), encoding="utf-8")


def test_create_version_is_born_unpublished(tmp_path: Path) -> None:
    _seed_working_copy(tmp_path)
    meta = versioning.create_version(tmp_path, message="first", reviewer="local")
    assert meta["published"] is False
    assert meta["published_at"] is None
    assert versioning.version_is_published(meta) is False


def test_legacy_meta_without_flag_reads_published() -> None:
    legacy = {"id": "20250101T000000", "message": "old"}
    assert versioning.version_is_published(legacy) is True


def test_publish_version_stamps_and_is_idempotent(tmp_path: Path) -> None:
    _seed_working_copy(tmp_path)
    created = versioning.create_version(tmp_path, message="v", reviewer="local")
    published = versioning.publish_version(tmp_path, created["id"], reviewer="human")
    assert published["published"] is True
    assert published["published_by"] == "human"
    assert isinstance(published["published_at"], str)
    again = versioning.publish_version(tmp_path, created["id"], reviewer="other")
    assert again["published_by"] == "human"  # first stamp wins; idempotent
    on_disk = versioning.load_version_meta(tmp_path, created["id"])
    assert on_disk["published"] is True


def test_publish_missing_version_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        versioning.publish_version(tmp_path, "20990101T000000", reviewer="human")
