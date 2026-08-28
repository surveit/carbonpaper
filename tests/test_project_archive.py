"""A project archive's refusal path; its round trip is covered in test_admin_ui.py."""
from __future__ import annotations

from io import BytesIO
import json
import zipfile

import pytest

from app.core.persistence import configure_store
from app.core.sqlite_store import SqliteKvStore
from app.services import project, workspace
from app.services.errors import ProjectArchiveRejected
from app.services.project import export_project_archive, import_project_archive


@pytest.fixture(autouse=True)
def workspace_root(tmp_path):
    examples = tmp_path / "examples"
    examples.mkdir()
    workspace.set_projects_dir(examples)
    configure_store(SqliteKvStore(":memory:"))
    return examples


def _exported_archive() -> bytes:
    source = project.create_project(
        "Insulin Cap Filings", "Triage the filings that contradict a commitment.",
        model="sonnet", source="test",
    ).id
    return export_project_archive(source)


def test_a_cache_from_another_key_version_is_refused_before_a_project_is_written():
    archive = _replace_manifest(_exported_archive(), cache_key_version=1)
    already_there = project.list_projects()

    with pytest.raises(ProjectArchiveRejected, match="v1 cache keys"):
        import_project_archive(archive)

    # A project written here would be one nobody asked for, holding no cache.
    assert project.list_projects() == already_there



def _replace_manifest(archive: bytes, *, cache_key_version: int) -> bytes:
    out = BytesIO()
    with zipfile.ZipFile(BytesIO(archive)) as source, zipfile.ZipFile(out, "w") as rebuilt:
        for name in source.namelist():
            payload = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(payload)
                manifest["cache_key_version"] = cache_key_version
                payload = json.dumps(manifest).encode("utf-8")
            rebuilt.writestr(name, payload)
    return out.getvalue()
