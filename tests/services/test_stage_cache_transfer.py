"""Isolated by the autouse in-memory store and temp frame store, so nothing here
touches the real workspace.
"""
from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from app.core.stage_cache import CACHE_KEY_VERSION, StageCacheEntry
from app.models import Column, StageType
from app.models.stages.input_data import Connector, ConnectorKind, InputDataStage, FileConnectorParams, FileFormat
from app.models.stages.signature import ReplacesSignature
from app.services import loader, project
from app.services.loader import save_stages
from app.services.stage_cache_transfer import (
    CacheArchiveRejected, StageImportCount, count_cached_entries, export_stage_cache,
    import_stage_cache,
)

_SOURCE = "20260819T124525.966743"
_STAGE = "llm_classification"


def _record(project_id: str, *, stage_fingerprint: str, input_fingerprint: str, verdict: bool) -> None:
    StageCacheEntry.read_write().record(
        project_id=project_id,
        stage_id=_STAGE,
        stage_fingerprint=stage_fingerprint,
        input_fingerprint=input_fingerprint,
        input_row={"comment": "Diese Klimakleber sind eine Plage"},
        output_row={"is_abusive": verdict, "category": 9},
        branches=["classify/0:if"],
    )


def test_export_then_import_moves_entries_under_the_destination_project():
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    archive = export_stage_cache(_SOURCE)

    report = import_stage_cache(archive, "destination")

    assert report.written == 1
    assert report.source_project == _SOURCE
    moved = StageCacheEntry.read_only().get("destination", _STAGE, "fp_a", "row_1")
    assert moved is not None
    assert moved.output_row == {"is_abusive": True, "category": 9}
    assert moved.branches == ["classify/0:if"]
    assert moved.project == "destination"


def test_a_row_carrying_a_unicode_line_separator_survives_the_round_trip():
    """U+2028 is a line break to str.splitlines but not to json.dumps, so it splits a record."""
    StageCacheEntry.read_write().record(
        project_id=_SOURCE, stage_id=_STAGE, stage_fingerprint="fp_a",
        input_fingerprint="row_1",
        input_row={"specific_issues": "Issues related to AI.\u2028Trade promotion."},
        output_row={"is_abusive": False, "category": 1}, branches=[],
    )

    report = import_stage_cache(export_stage_cache(_SOURCE), "destination")

    assert report.written == 1
    moved = StageCacheEntry.read_only().get("destination", _STAGE, "fp_a", "row_1")
    assert moved is not None
    assert moved.output_row == {"is_abusive": False, "category": 1}


def test_the_source_project_keeps_its_own_entries():
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)

    import_stage_cache(export_stage_cache(_SOURCE), "destination")

    assert count_cached_entries(_SOURCE) == 1


def test_an_entry_already_stored_is_left_alone():
    """Append-only matters because the two outputs can differ: they are model answers."""
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    archive = export_stage_cache(_SOURCE)
    _record("destination", stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=False)

    report = import_stage_cache(archive, "destination")

    assert (report.written, report.already_stored) == (0, 1)
    held = StageCacheEntry.read_only().get("destination", _STAGE, "fp_a", "row_1")
    assert held is not None
    assert held.output_row == {"is_abusive": False, "category": 9}


def test_importing_twice_writes_nothing_the_second_time():
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    archive = export_stage_cache(_SOURCE)

    import_stage_cache(archive, "destination")
    second = import_stage_cache(archive, "destination")

    assert (second.written, second.already_stored) == (0, 1)


def test_only_the_named_project_is_exported():
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    _record("other_project", stage_fingerprint="fp_a", input_fingerprint="row_2", verdict=True)

    report = import_stage_cache(export_stage_cache(_SOURCE), "destination")

    assert report.written == 1


def test_an_archive_at_another_cache_key_version_is_refused():
    """Refused, not imported: every entry would be stored and never read."""
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    stale = _rewrite_manifest_version(export_stage_cache(_SOURCE), CACHE_KEY_VERSION - 1)

    with pytest.raises(CacheArchiveRejected, match=f"reads v{CACHE_KEY_VERSION}"):
        import_stage_cache(stale, "destination")

    assert count_cached_entries("destination") == 0


def test_a_zip_that_is_not_a_cache_export_is_refused():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "unrelated")

    with pytest.raises(CacheArchiveRejected, match="not a stage-cache export"):
        import_stage_cache(buffer.getvalue(), "destination")


def test_an_export_carries_no_frame_members():
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    with zipfile.ZipFile(BytesIO(export_stage_cache(_SOURCE))) as bundle:
        assert not [n for n in bundle.namelist() if n.startswith("frames/")]


def test_frame_members_from_an_older_export_are_counted_and_left_behind():
    """The row entries beside them are still worth importing, so the archive is not refused."""
    _record(_SOURCE, stage_fingerprint="fp_a", input_fingerprint="row_1", verdict=True)
    older = _add_frame_member(export_stage_cache(_SOURCE), "v4/proj/stage/fp_a/inputfp")

    report = import_stage_cache(older, "destination")

    assert report.frames_skipped == 1
    assert report.written == 1
    assert StageCacheEntry.read_only().get("destination", _STAGE, "fp_a", "row_1") is not None


def _add_frame_member(archive: bytes, frame_id: str) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(archive)) as source:
        with zipfile.ZipFile(buffer, "w") as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
            target.writestr(f"frames/{frame_id}.parquet", b"not read, only counted")
    return buffer.getvalue()


# ── reachability ──────────────────────────────────────────────────────────────

@pytest.fixture
def destination_project() -> str:
    """A real project with a real stage: reachability is measured against parsed stages."""
    project_id = project.create_project(
        "destination", "Trace the shell companies.", source="test").id
    save_stages(project_id, [InputDataStage(
        id="load_entities", description="Load Entities", type=StageType.input_data,
        connector=Connector(kind=ConnectorKind.file,
                            params=FileConnectorParams(format=FileFormat.csv)),
        signature=ReplacesSignature(produces=[
            Column(name="entity_id", type="str", nullable=False),
        ]),
    )])
    return project_id


def _fingerprint_of_first_stage(project_id: str) -> tuple[str, str]:
    [stage, *_] = loader.list_parsed_stages(loader.load_stage_entries(project_id))
    return stage.id, stage.compute_definition_fingerprint()


def test_entries_matching_a_live_stage_definition_count_as_reachable(destination_project):
    stage_id, fingerprint = _fingerprint_of_first_stage(destination_project)
    StageCacheEntry.read_write().record(
        project_id=_SOURCE, stage_id=stage_id, stage_fingerprint=fingerprint,
        input_fingerprint="row_1", input_row={"x": 1}, output_row={"y": 2},
        branches=None,
    )

    report = import_stage_cache(export_stage_cache(_SOURCE), destination_project)

    assert report.reachable == 1
    assert report.stages == [StageImportCount(stage_id=stage_id, imported=1, reachable=1)]


def test_entries_from_an_edited_stage_import_but_are_not_reachable(destination_project):
    stage_id, _ = _fingerprint_of_first_stage(destination_project)
    StageCacheEntry.read_write().record(
        project_id=_SOURCE, stage_id=stage_id, stage_fingerprint="fingerprint_from_an_older_edit",
        input_fingerprint="row_1", input_row={"x": 1}, output_row={"y": 2},
        branches=None,
    )

    report = import_stage_cache(export_stage_cache(_SOURCE), destination_project)

    assert report.written == 1
    assert report.reachable == 0


def _rewrite_manifest_version(archive: bytes, version: int) -> bytes:
    """Builds the archive a workspace on older code would have written."""
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(archive)) as source:
        with zipfile.ZipFile(buffer, "w") as target:
            for name in source.namelist():
                raw = source.read(name)
                if name == "manifest.json":
                    raw = raw.replace(
                        f'"cache_key_version": {CACHE_KEY_VERSION}'.encode(),
                        f'"cache_key_version": {version}'.encode(),
                    )
                target.writestr(name, raw)
    return buffer.getvalue()
