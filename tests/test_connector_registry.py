"""A pack's connector kind: params read by its own model, and one stored Source per row."""
from __future__ import annotations

import dataclasses
import io
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

import pytest
from pydantic import ValidationError

import app.packs
from app.core.errors import MissingInputBindingError, SourceUnavailable
from app.core.files import list_project_files, resolve_stored_path, save_upload
from app.core.frames import read_frame_table
from app.models import parse_stage
from app.models.connectors import (
    CONNECTORS, SOURCE_COLUMNS, AcquiredBytes, ConnectorParams, ConnectorSpec,
)
from app.models.packs import PACKS, PackSpec, register_pack
from app.models.run_manifest import index_bound_sources
from app.models.schema import Column
from app.models.stages.input_data import Connector
from app.runtime.context import PrepareScope, RunContext
from app.runtime.runner import execute_run
from app.runtime.stages.input_data import acquire_input_data, read_input_data
from conftest import pinned_stages, place_stage
from stage_seed import add_stage, save_version

_ORIGIN = "https://example.org/letters/"
_KIND = "folder_files"
_STEM = Column(name="stem", type="str", nullable=True)


class FolderParams(ConnectorParams):
    folder: str


def _acquire_folder(params: FolderParams) -> Iterator[AcquiredBytes]:
    folder = Path(params.folder)
    if not folder.is_dir():
        raise SourceUnavailable(f"no folder at {folder}")
    for path in sorted(folder.iterdir()):
        yield AcquiredBytes(filename=path.name, origin_url=_ORIGIN + path.name,
                            open_bytes=_opener(path),
                            metadata={"stem": path.stem})


def _opener(path: Path) -> Callable[[], BinaryIO]:
    return lambda: path.open("rb")


_FOLDER = ConnectorSpec(kind=_KIND, params_model=FolderParams, metadata_columns=(_STEM,),
                        acquire=_acquire_folder)


@pytest.fixture
def folder_pack() -> Iterator[None]:
    saved_connectors, saved_packs = dict(CONNECTORS), dict(PACKS)
    register_pack(PackSpec(pack_id="letters", connectors=(_FOLDER,)))
    yield
    CONNECTORS.clear()
    CONNECTORS.update(saved_connectors)
    PACKS.clear()
    PACKS.update(saved_packs)


def _letters_stage(params: dict) -> dict:
    produces = [column.model_dump(mode="json", exclude_defaults=True)
                for column in (*SOURCE_COLUMNS, _STEM)]
    return {"id": "letters", "description": "Read the letters", "type": "input_data",
            "connector": {"kind": _KIND, "params": params},
            "signature": {"form": "replaces", "produces": produces}}


def _write_letters(tmp_path: Path) -> Path:
    folder = tmp_path / "letters"
    folder.mkdir()
    (folder / "first.txt").write_bytes(b"Dear council,")
    (folder / "second.txt").write_bytes(b"To the board,")
    return folder


def _run_letters(tmp_path: Path, params: dict, bindings: dict | None = None) -> dict:
    add_stage(tmp_path, _letters_stage(params))
    save_version(tmp_path.name, message="seed")
    return execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path),
                       bindings=bindings)


def _read_output(tmp_path: Path, manifest: dict) -> list[dict]:
    path = tmp_path / "runs" / manifest["run_id"] / "outputs" / "letters.parquet"
    return read_frame_table(path).to_pylist()


# ── the kind and its params ──────────────────────────────────────────────────

def test_a_kind_no_pack_registers_is_refused() -> None:
    with pytest.raises(ValidationError, match=f"'{_KIND}' is not 'file' and no pack registers it"):
        Connector.model_validate({"kind": _KIND, "params": {"folder": "/letters"}})


def test_a_registered_kind_reads_its_params_with_its_own_model(folder_pack) -> None:
    connector = Connector.model_validate({"kind": _KIND, "params": {"folder": "/letters"}})
    assert isinstance(connector.params, FolderParams)
    assert Connector.model_validate(connector.model_dump()) == connector
    with pytest.raises(ValidationError, match="format"):
        Connector.model_validate({"kind": _KIND, "params": {"folder": "/l", "format": "csv"}})
    with pytest.raises(ValidationError, match="params.folder\n  Field required"):
        Connector.model_validate({"kind": _KIND})


@pytest.mark.parametrize(("pack_id", "connector", "refusal"), [
    ("letters", dataclasses.replace(_FOLDER, kind="notes"), "pack 'letters' is already"),
    ("other", _FOLDER, f"kind\\(s\\) \\['{_KIND}'\\]"),
    ("other", dataclasses.replace(_FOLDER, kind="file"), "kind\\(s\\) \\['file'\\]"),
    ("other", dataclasses.replace(_FOLDER, kind="notes", metadata_columns=(
        Column(name="_row", type="int", nullable=True),)), "'_row'"),
    ("other", dataclasses.replace(_FOLDER, kind="notes", metadata_columns=(
        Column(name="filename", type="str", nullable=True),)), "'filename'"),
])
def test_register_pack_refuses_a_name_already_held(
    folder_pack, pack_id: str, connector: ConnectorSpec, refusal: str,
) -> None:
    held = (dict(PACKS), dict(CONNECTORS))
    with pytest.raises(ValueError, match=refusal):
        register_pack(PackSpec(pack_id=pack_id, connectors=(connector,)))
    assert (dict(PACKS), dict(CONNECTORS)) == held


def test_importing_app_packs_registers_one_pack_per_directory_named_for_it() -> None:
    root = Path(app.packs.__file__).parent
    directories = {init.parent.name for init in root.glob("*/__init__.py")
                   if not init.parent.name.startswith("_")}
    assert set(PACKS) == directories


# ── a run over the kind ──────────────────────────────────────────────────────

def test_a_run_stores_each_acquired_file_and_reads_one_row_per_file(folder_pack, tmp_path) -> None:
    manifest = _run_letters(tmp_path, {"folder": str(_write_letters(tmp_path))})

    assert manifest["status"] == "ok"
    stored = {record.filename: record for record in list_project_files(tmp_path.name)}
    assert {name: record.origin_url for name, record in stored.items()} == {
        "first.txt": _ORIGIN + "first.txt", "second.txt": _ORIGIN + "second.txt"}
    assert _read_output(tmp_path, manifest) == [
        {"source_id": stored[name].id, "source_sha256": stored[name].sha256,
         "filename": name, "origin_url": _ORIGIN + name,
         "fetched_at": stored[name].fetched_at, "stem": Path(name).stem}
        for name in ("first.txt", "second.txt")]
    read = manifest["input_bindings"]["letters"]["files"]
    assert [(one["file_id"], one["path"]) for one in read] == [
        (stored[name].id, str(resolve_stored_path(stored[name])))
        for name in ("first.txt", "second.txt")]


def test_each_row_names_the_stored_file_it_came_from(folder_pack, tmp_path) -> None:
    workflow_stage = place_stage(parse_stage(
        _letters_stage({"folder": str(_write_letters(tmp_path))})))
    scope = PrepareScope(project_id=tmp_path.name, run_dir=tmp_path / "run")
    record = acquire_input_data(workflow_stage, scope)
    assert record is not None
    bound = index_bound_sources({"letters": record.model_dump(mode="json")})

    output = read_input_data(workflow_stage, RunContext.for_workflow_test_run(
        scope.run_dir, scope.project_id, "run", bound_sources=bound))

    assert output.lineage is not None
    parents = [parent for parents in output.lineage.parents for parent in parents]
    stored = sorted(list_project_files(tmp_path.name), key=lambda record: record.filename)
    assert [(p.row_ordinal, p.source_file, p.source_file_sha) for p in parents] == [
        (0, str(resolve_stored_path(record)), record.sha256) for record in stored]


def test_a_bound_stored_file_wins_over_acquiring(folder_pack, tmp_path) -> None:
    uploaded = save_upload("third.txt", io.BytesIO(b"Minutes,"), tmp_path.name)

    manifest = _run_letters(
        tmp_path, {"folder": str(tmp_path / "no-such-folder")},
        bindings={"letters": {"paths": [str(resolve_stored_path(uploaded))]}})

    assert manifest["status"] == "ok"
    assert _read_output(tmp_path, manifest) == [
        {"source_id": uploaded.id, "source_sha256": uploaded.sha256, "filename": "third.txt",
         "origin_url": None, "fetched_at": None, "stem": None}]


def test_a_bound_file_outside_the_project_store_is_refused(folder_pack, tmp_path) -> None:
    loose = tmp_path / "loose.txt"
    loose.write_bytes(b"Minutes,")
    with pytest.raises(MissingInputBindingError, match="not one of this project's stored"):
        _run_letters(tmp_path, {"folder": str(tmp_path)},
                     bindings={"letters": {"paths": [str(loose)]}})


def test_a_source_the_connector_cannot_reach_refuses_the_run(folder_pack, tmp_path) -> None:
    with pytest.raises(MissingInputBindingError, match="`letters`: no folder at"):
        _run_letters(tmp_path, {"folder": str(tmp_path / "no-such-folder")})
    assert not (tmp_path / "runs").exists()


def test_metadata_off_the_declaration_stops_the_file_being_stored(folder_pack, tmp_path) -> None:
    pages = Column(name="pages", type="int", nullable=True)
    # folder_pack restores the registry after the test.
    CONNECTORS[_KIND] = dataclasses.replace(_FOLDER, metadata_columns=(_STEM, pages))
    with pytest.raises(ValueError, match="gave 'first.txt' metadata \\['stem'\\]"):
        _run_letters(tmp_path, {"folder": str(_write_letters(tmp_path))})
    assert list_project_files(tmp_path.name) == []


def test_the_handler_refuses_a_run_that_skipped_acquiring(folder_pack, tmp_path) -> None:
    workflow_stage = place_stage(parse_stage(_letters_stage({"folder": str(tmp_path)})))
    with pytest.raises(ValueError, match="no source table in this run"):
        read_input_data(workflow_stage, RunContext.for_workflow_test_run(
            tmp_path / "run", tmp_path.name, "run", bound_sources={}))
