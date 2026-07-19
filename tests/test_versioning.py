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
    assert meta.published is False
    assert meta.published_at is None


def test_legacy_meta_without_flag_reads_published(tmp_path: Path) -> None:
    """A version.json written before the `published` flag existed was created only
    by the human "Create version" act, so load_version_meta grandfathers a missing
    key in as published."""
    vdir = tmp_path / "versions" / "20250101T000000"
    vdir.mkdir(parents=True)
    (vdir / "version.json").write_text(
        json.dumps({
            "id": "20250101T000000", "created_at": "2025-01-01T00:00:00",
            "parent_version": None, "message": "old", "reviewer": "local",
            "coverage": {"approved": 0, "rejected": 0, "edited_stale": 0,
                         "unreviewed": 0, "total": 0, "approved_pct": 0.0},
        }),
        encoding="utf-8",
    )
    meta = versioning.load_version_meta(tmp_path, "20250101T000000")
    assert meta.published is True


def test_publish_version_stamps_and_is_idempotent(tmp_path: Path) -> None:
    _seed_working_copy(tmp_path)
    created = versioning.create_version(tmp_path, message="v", reviewer="local")
    published = versioning.publish_version(tmp_path, created.id, reviewer="human")
    assert published.published is True
    assert published.published_by == "human"
    assert isinstance(published.published_at, str)
    again = versioning.publish_version(tmp_path, created.id, reviewer="other")
    assert again.published_by == "human"  # first stamp wins; idempotent
    on_disk = versioning.load_version_meta(tmp_path, created.id)
    assert on_disk.published is True


def test_publish_missing_version_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        versioning.publish_version(tmp_path, "20990101T000000", reviewer="human")


def test_create_version_from_stages_writes_a_loadable_version(tmp_path: Path) -> None:
    meta = versioning.create_version_from_stages(
        tmp_path, [_STAGE], message="from agent", reviewer="agent",
        parent_version=None,
    )
    stages = versioning.load_version_stages(tmp_path, meta.id)
    assert [s.id for s in stages] == ["load"]
    assert meta.published is False
    assert meta.parent_version is None
    assert meta.reviewer == "agent"


def test_create_version_from_invalid_stages_writes_nothing(tmp_path: Path) -> None:
    import pydantic

    broken = dict(_STAGE, id="dangling", inputs=[{"id": "missing"}])
    with pytest.raises(pydantic.ValidationError):
        versioning.create_version_from_stages(
            tmp_path, [broken], message="bad", reviewer="agent",
        )
    assert versioning.list_versions(tmp_path) == []


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "not-a-version"])
def test_load_version_meta_rejects_non_timestamp_id(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(FileNotFoundError):
        versioning.load_version_meta(tmp_path, bad_id)


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "not-a-version"])
def test_load_version_stages_rejects_non_timestamp_id(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(FileNotFoundError):
        versioning.load_version_stages(tmp_path, bad_id)


def test_create_version_no_longer_snapshots_schemas(tmp_path: Path) -> None:
    _seed_working_copy(tmp_path)
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "01_docs.json").write_text("{}", encoding="utf-8")
    meta = versioning.create_version(tmp_path, message="v", reviewer="local")
    vdir = tmp_path / "versions" / meta.id
    assert (vdir / "compiled").is_dir()
    assert not (vdir / "schemas").exists()
