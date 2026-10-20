"""A connector marks a source it could not reach as a row with no file."""
from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import SourceUnavailable
from app.core.frames import read_frame_table
from app.core.files import list_project_files, resolve_stored_path
from app.models import WorkflowStage, parse_stage
from app.models.connectors import (
    CONNECTORS, SOURCE_COLUMNS, AcquiredBytes, ConnectorParams, ConnectorSpec, UnreachedSource,
)
from app.models.run_manifest import index_bound_sources
from app.models.packs import PACKS, PackSpec, register_pack
from app.models.schema import Column
from app.runtime.context import PrepareScope, RunContext
from app.runtime.stages.input_data import AcquiredStage, acquire_input_data, read_input_data
from app.runtime.stages.read_pages import handle_read_pages
from conftest import as_inputs, place_stage, reads_of

_KIND = "unreach_test"
_STATUS = Column(name="fetch_status", type="str", nullable=True)
_REACHED_URL = "https://x.test/reached"
_UNREACHED_URL = "https://x.test/a"
_Item = AcquiredBytes | UnreachedSource


def _reached() -> AcquiredBytes:
    return AcquiredBytes(
        filename="reached.txt", origin_url=_REACHED_URL,
        open_bytes=lambda: io.BytesIO(b"reached"), metadata={"fetch_status": "ok"})


def _unreached(metadata: dict | None = None) -> UnreachedSource:
    return UnreachedSource(
        requested_url=_UNREACHED_URL,
        metadata={"fetch_status": "http_404"} if metadata is None else metadata)


@pytest.fixture
def register_items() -> Iterator[Callable[[list[_Item]], None]]:
    saved_connectors, saved_packs = dict(CONNECTORS), dict(PACKS)

    def register(items: list[_Item]) -> None:
        register_pack(PackSpec(pack_id="unreach_pack", connectors=(ConnectorSpec(
            kind=_KIND, params_model=ConnectorParams, metadata_columns=(_STATUS,),
            acquire=lambda params: iter(items)),)))

    yield register
    CONNECTORS.clear()
    CONNECTORS.update(saved_connectors)
    PACKS.clear()
    PACKS.update(saved_packs)


def _pages_stage() -> WorkflowStage:
    produces = [column.model_dump(mode="json", exclude_defaults=True)
                for column in (*SOURCE_COLUMNS, _STATUS)]
    return place_stage(parse_stage({
        "id": "pages", "description": "Read the pages", "type": "input_data",
        "connector": {"kind": _KIND, "params": {}},
        "signature": {"form": "replaces", "produces": produces}}))


def _acquire(tmp_path: Path) -> AcquiredStage | None:
    scope = PrepareScope(project_id=tmp_path.name, run_dir=tmp_path / "run")
    return acquire_input_data(_pages_stage(), scope)


def test_unreached_source_becomes_a_row_with_null_source_columns(
    register_items, tmp_path: Path,
) -> None:
    register_items([_reached(), _unreached()])

    acquired = _acquire(tmp_path)

    assert acquired is not None
    acquired.write()
    rows = read_frame_table(acquired.table_path).to_pylist()
    assert len(rows) == 2
    assert rows[1] == {
        "source_id": None, "source_sha256": None, "filename": None, "origin_url": _UNREACHED_URL,
        "fetched_at": None, "fetch_status": "http_404"}
    assert rows[0]["source_id"] is not None and rows[0]["fetch_status"] == "ok"
    assert len(acquired.record.files) == 1


def test_a_run_whose_every_source_is_unreached_is_refused(register_items, tmp_path: Path) -> None:
    register_items([_unreached(), _unreached()])

    with pytest.raises(SourceUnavailable, match="acquired no file"):
        _acquire(tmp_path)


def test_unreached_metadata_must_match_declared_columns(register_items, tmp_path: Path) -> None:
    register_items([_reached(), _unreached(metadata={})])

    with pytest.raises(ValueError, match=r"\['fetch_status'\]"):
        _acquire(tmp_path)


def test_read_pages_refuses_a_null_source_id(tmp_path: Path) -> None:
    columns = [{"name": "source_id", "type": "str", "nullable": True},
               {"name": "source_sha256", "type": "str", "nullable": True}]
    stage = place_stage(parse_stage({
        "id": "filing_pages", "description": "Read each filing a page at a time",
        "type": "read_pages", "inputs": [{"id": "filings"}], "row_type_id": "filing_page",
        "signature": {"form": "replaces", "reads": reads_of("filings", columns),
                      "produces": [*columns,
                                   {"name": "page", "type": "int", "nullable": False},
                                   {"name": "page_text", "type": "str", "nullable": False},
                                   {"name": "page_span", "type": "span", "nullable": False}]},
        "read_pages": {"carry": []}}))
    ctx = RunContext.for_workflow_test_run(tmp_path, "proj", "run", bound_sources={})
    sources = pd.DataFrame([{"source_id": None, "source_sha256": None}])

    with pytest.raises(ValueError, match=r"filing_pages.*fetch_status|fetch_status.*filing_pages"):
        handle_read_pages(stage, as_inputs({"filings": sources}), ctx)


def test_an_unreached_row_has_no_parent_file_in_its_lineage(register_items, tmp_path: Path) -> None:
    register_items([_reached(), _unreached()])
    acquired = _acquire(tmp_path)
    assert acquired is not None
    acquired.write()
    bound = index_bound_sources({"pages": acquired.record.model_dump(mode="json")})
    ctx = RunContext.for_workflow_test_run(
        tmp_path / "run", tmp_path.name, "run", bound_sources=bound)

    output = read_input_data(_pages_stage(), ctx)

    assert output.lineage is not None
    stored = list_project_files(tmp_path.name)
    assert len(stored) == 1
    parents = output.lineage.parents
    assert [(p.row_ordinal, p.source_file, p.source_file_sha) for p in parents[0]] == [
        (0, str(resolve_stored_path(stored[0])), stored[0].sha256)]
    assert parents[1] == []
