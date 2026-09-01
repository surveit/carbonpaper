"""What the run page reads off the workflow around a version's authored review guide. Project
scoping is by directory name under a repointed projects dir, isolated per test by
the autouse in-memory store (conftest.fresh_store)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest


from app.models import parse_stage
from app.models.review_guide import ReviewGuideStep
from app.models.records.review_guide import ReviewGuide
from app.services import workspace
from app.models.workflow import parse_workflow
from app.models.workflow_stage import WorkflowStage
from app.services.run_guide import build_run_guide_view, list_written_columns
from app.services.versioning import (
    create_version_from_stages,
    find_latest_review_guide,
    save_version_guide,
)

_DOC_ID = {"name": "doc_id", "type": "str", "nullable": False}
_FLAG = {"name": "flag", "type": "bool", "nullable": False}
_SOURCE = {"name": "source", "type": "str", "nullable": False}

_ROWS = {"columns": [_DOC_ID]}
_SOURCES = {"columns": [_DOC_ID, _SOURCE]}
_FLAGGED = {"columns": [_DOC_ID, _FLAG]}
_ATTACHED = {"columns": [_DOC_ID, _FLAG, _SOURCE]}

_ROW_FUNCTION = {"kind": "inline", "code": "def transform(row):\n    return row\n"}

_RUN_ID = "20260101T000000"

# load_rows → add_flag → keep_flagged ─┐
#                        load_sources ─┴→ attach_source
_STAGES: list[dict[str, Any]] = [
    {"id": "load_rows", "description": "Load rows", "type": "input_data",
     "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _ROWS["columns"]}},
    {"id": "load_sources", "description": "Load sources", "type": "input_data",
     "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _SOURCES["columns"]}},
    {"id": "add_flag", "description": "Flag rows", "type": "python_row_function",
     "inputs": [{"id": "load_rows"}],
     "function": _ROW_FUNCTION, "signature": {
         "form": "extends",
         "reads": [{"input": "load_rows", "columns": _ROWS["columns"]}],
         "adds": [_FLAG],
     }},
    {"id": "keep_flagged", "description": "Keep the flagged rows", "type": "filter_rows",
     "inputs": [{"id": "add_flag"}],
     "filter": {"code": "def should_include(row):\n    return row['flag']\n"},
     "signature": {"form": "extends",
                   "reads": [{"input": "add_flag", "columns": [_FLAG]}]}},
    {"id": "attach_source", "description": "Attach the source", "type": "enrich",
     "inputs": [{"id": "keep_flagged"},
                {"id": "load_sources"}],
     "join": {"keys": [{"left": "doc_id", "right": "doc_id"}], "enrich_with": {"source": "source"}},
     "signature": {
         "form": "extends",
         "reads": [
             {"input": "keep_flagged", "columns": _ROWS["columns"]},
             {"input": "load_sources", "columns": _ROWS["columns"]},
         ],
         "adds": [_SOURCE],
     }},
]

# attach_source is named first, though the run reaches it last.
_STEPS = [
    ReviewGuideStep(title="Read the rows", prose="Reads every `doc_id` filed.",
                    stage_ids=["load_rows"],
                    data_description="Every row the source filed, as filed."),
    ReviewGuideStep(title="Decide what counts",
                    prose="A row is kept on `flag`, then given its `source`.",
                    stage_ids=["attach_source", "add_flag"],
                    data_description="The kept rows, each carrying the source it came from."),
]
_UNNARRATED = ["load_sources", "keep_flagged"]


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    examples = tmp_path / "examples"
    project = examples / "demo"
    project.mkdir(parents=True)
    workspace.set_projects_dir(examples)
    return project


# What a run of _STAGES measured: 10 rows in, one dropped to 4 by the filter, the rest
# passing their rows through. Keyed by stage id, as the manifest's records are.
_COUNTS = {
    "load_rows": 10, "add_flag": 10, "keep_flagged": 4,
    "load_sources": 3, "attach_source": 4,
}


# The frames such a run WROTE, as column labels. Wider than the stages declare:
# `keep_flagged` and `attach_source` carry `note` through without naming it, which is
# what makes the declared schema the wrong place to read a frame's width from.
_WRITTEN_COLUMNS = {
    "load_rows": ["doc_id", "note"],
    "load_sources": ["doc_id", "source"],
    "add_flag": ["doc_id", "note", "flag"],
    "keep_flagged": ["doc_id", "note", "flag"],
    "attach_source": ["doc_id", "note", "flag", "source"],
}


def _manifest(
    version_id: str,
    *,
    executed: list[str] | None = None,
    counts: dict[str, int] | None = None,
) -> dict:
    ran = _STAGES if executed is None else [s for s in _STAGES if s["id"] in executed]
    measured = _COUNTS if counts is None else counts
    return {
        "run_id": _RUN_ID,
        "workflow_version": version_id,
        "stage_records": [
            {"stage_id": s["id"], "status": "ok",
             "output_row_count": measured.get(s["id"]),
             "output_path": f"outputs/{s['id']}.parquet"}
            for s in ran
        ],
    }


def _write_run_outputs(project_dir: Path, *, columns: dict[str, list[str]] | None = None):
    outputs = project_dir / "runs" / _RUN_ID / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    for stage_id, names in (columns if columns is not None else _WRITTEN_COLUMNS).items():
        frame = pd.DataFrame({name: ["x"] * _COUNTS[stage_id] for name in names})
        frame.to_parquet(outputs / f"{stage_id}.parquet", index=False)


def _stages_by_id(view) -> dict:
    return {
        s.stage_id: s
        for s in [*view.steps[0].stages, *view.steps[1].stages, *view.unnarrated]
    }


def _version_with_guide(project_dir: Path, **overrides) -> str:
    version = create_version_from_stages(project_dir.name, _STAGES, message="v1"
    )
    guide = ReviewGuide(
        project=project_dir.name,
        version_id=version.version_id,
        steps=overrides.get("steps", _STEPS),
        unnarrated=overrides.get("unnarrated", _UNNARRATED),
    )
    save_version_guide(project_dir.name, version.version_id, guide)
    return version.version_id


# ── no guide, no panel ───────────────────────────────────────────────────────

def test_no_view_when_the_pinned_version_carries_no_guide(project_dir):
    version = create_version_from_stages(project_dir.name, _STAGES, message="v1"
    )
    assert build_run_guide_view("demo", _manifest(version.version_id)) is None


def test_no_view_when_the_manifest_records_no_version(project_dir):
    assert build_run_guide_view("demo", {"stage_records": []}) is None


def test_no_view_when_the_pinned_version_cannot_be_read(project_dir):
    _version_with_guide(project_dir)
    assert build_run_guide_view("demo", _manifest("20200101T000000")) is None


# ── the facts read off the stages ────────────────────────────────────────────

def test_a_step_carries_its_authored_prose_and_title(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert [step.title for step in view.steps] == ["Read the rows", "Decide what counts"]
    assert view.steps[0].prose == "Reads every `doc_id` filed."


def test_a_steps_stages_come_back_in_execution_order_not_guide_order(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert [s.stage_id for s in view.steps[1].stages] == ["add_flag", "attach_source"]


def test_each_stage_carries_its_definition_from_the_pinned_version(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    [loaded] = view.steps[0].stages
    assert loaded.workflow_stage.stage.description == "Load rows"
    assert loaded.workflow_stage.stage.type == "input_data"


def test_written_columns_are_read_off_each_stage(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    written = {s.stage_id: s.written_columns for s in _stages_by_id(view).values()}
    assert written == {
        "load_rows": ["doc_id"],
        "load_sources": ["doc_id", "source"],
        "add_flag": ["flag"],
        "keep_flagged": [],
        "attach_source": ["source"],
    }


def test_unnarrated_stages_are_carried_in_execution_order(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert [s.stage_id for s in view.unnarrated] == ["load_sources", "keep_flagged"]


def test_a_stage_this_run_did_not_execute_is_flagged_not_dropped(project_dir):
    manifest = _manifest(
        _version_with_guide(project_dir), executed=["load_rows", "add_flag"]
    )
    view = build_run_guide_view("demo", manifest)

    executed = {s.stage_id: s.executed for s in _stages_by_id(view).values()}
    assert executed == {
        "load_rows": True, "add_flag": True,
        "load_sources": False, "keep_flagged": False, "attach_source": False,
    }


def test_a_stage_id_the_version_does_not_define_is_kept_unresolved(project_dir):
    version_id = _version_with_guide(project_dir)
    # Saved past save_version_guide, which would have rejected the unknown id.
    ReviewGuide(
        project=project_dir.name,
        version_id=version_id,
        steps=[ReviewGuideStep(title="Read the rows", prose="Reads every `doc_id`.",
                               stage_ids=["load_rows", "renamed_away"])],
        unnarrated=["load_sources", "add_flag", "keep_flagged", "attach_source"],
    ).save()

    view = build_run_guide_view("demo", _manifest(version_id))

    [known, unresolved] = view.steps[0].stages
    assert known.stage_id == "load_rows" and known.workflow_stage is not None
    assert unresolved.stage_id == "renamed_away"
    assert unresolved.workflow_stage is None
    assert unresolved.written_columns == []


# ── the row counts, measured off the run's own manifest ──────────────────────

def test_each_stage_carries_the_row_count_this_run_measured_for_it(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    counts = {s.stage_id: s.output_row_count for s in _stages_by_id(view).values()}
    assert counts == _COUNTS


def test_a_stage_the_run_has_no_record_for_has_an_unknown_count_not_zero(project_dir):
    manifest = _manifest(_version_with_guide(project_dir), executed=["load_rows"])
    view = build_run_guide_view("demo", manifest)

    assert _stages_by_id(view)["add_flag"].output_row_count is None


def test_a_record_carrying_no_count_leaves_the_count_unknown(project_dir):
    manifest = _manifest(_version_with_guide(project_dir))
    manifest["stage_records"][1]["output_row_count"] = None

    view = build_run_guide_view("demo", manifest)

    assert _stages_by_id(view)["load_sources"].output_row_count is None


def test_a_measured_empty_frame_reads_as_zero_not_as_unknown(project_dir):
    counts = {**_COUNTS, "keep_flagged": 0, "attach_source": 0}
    manifest = _manifest(_version_with_guide(project_dir), counts=counts)

    view = build_run_guide_view("demo", manifest)

    assert _stages_by_id(view)["keep_flagged"].output_row_count == 0


# ── the other half of the shape: the columns ─────────────────────────────────

def test_each_stage_carries_the_column_count_of_the_frame_the_run_wrote(project_dir):
    version_id = _version_with_guide(project_dir)
    _write_run_outputs(project_dir)

    view = build_run_guide_view("demo", _manifest(version_id))

    columns = {s.stage_id: s.column_count for s in _stages_by_id(view).values()}
    assert columns == {
        "load_rows": 2, "load_sources": 2, "add_flag": 3,
        "keep_flagged": 3, "attach_source": 4,
    }


def test_the_column_count_is_the_frames_width_not_the_declared_schemas(project_dir):
    version_id = _version_with_guide(project_dir)
    _write_run_outputs(project_dir)

    view = build_run_guide_view("demo", _manifest(version_id))

    kept = _stages_by_id(view)["keep_flagged"]
    placed = parse_workflow(_STAGES).find_workflow_stage("keep_flagged")
    assert len(placed.output_schema.columns) == 2
    assert kept.column_count == 3


def test_a_stage_whose_frame_is_missing_has_an_unknown_column_count(project_dir):
    version_id = _version_with_guide(project_dir)
    _write_run_outputs(project_dir)
    (project_dir / "runs" / _RUN_ID / "outputs" / "attach_source.parquet").unlink()

    view = build_run_guide_view("demo", _manifest(version_id))

    absent = _stages_by_id(view)["attach_source"]
    assert absent.column_count is None
    assert absent.output_row_count == 4


def test_an_unreadable_frame_leaves_the_column_count_unknown(project_dir):
    version_id = _version_with_guide(project_dir)
    _write_run_outputs(project_dir)
    path = project_dir / "runs" / _RUN_ID / "outputs" / "add_flag.parquet"
    path.write_bytes(b"not a parquet file")

    view = build_run_guide_view("demo", _manifest(version_id))

    assert _stages_by_id(view)["add_flag"].column_count is None


def test_the_two_halves_of_the_shape_are_measured_apart(project_dir):
    version_id = _version_with_guide(project_dir)
    _write_run_outputs(project_dir)
    manifest = _manifest(version_id)
    manifest["stage_records"][4]["output_row_count"] = None

    view = build_run_guide_view("demo", manifest)

    half = _stages_by_id(view)["attach_source"]
    assert half.output_row_count is None
    assert half.column_count == 4


def test_a_run_with_no_frames_on_disk_knows_no_column_counts(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert all(s.column_count is None for s in _stages_by_id(view).values())


def test_a_stage_the_version_does_not_define_has_neither_half(project_dir):
    version_id = _version_with_guide(project_dir)
    ReviewGuide(
        project=project_dir.name,
        version_id=version_id,
        steps=[ReviewGuideStep(title="Read the rows", prose="Reads every `doc_id`.",
                               stage_ids=["renamed_away"])],
        unnarrated=["load_rows", "load_sources", "add_flag", "keep_flagged",
                    "attach_source"],
    ).save()

    view = build_run_guide_view("demo", _manifest(version_id))

    [unresolved] = view.steps[0].stages
    assert unresolved.output_row_count is None
    assert unresolved.column_count is None


# ── the authored sentence on a Workflow section's data link ──────────────────

def test_a_section_carries_the_authored_sentence_about_its_data(project_dir):
    steps = [ReviewGuideStep(
        title="Read the rows", prose="Reads every `doc_id` filed.",
        stage_ids=["load_rows"],
        data_description="Every row the source filed, as filed.")]
    version_id = _version_with_guide(project_dir, steps=steps, unnarrated=[
        "load_sources", "add_flag", "keep_flagged", "attach_source",
    ])

    view = build_run_guide_view("demo", _manifest(version_id))

    assert view.steps[0].data_description == "Every row the source filed, as filed."


def _store_a_guide_written_before_the_field_existed(project_dir: Path) -> str:
    version_id = _version_with_guide(project_dir)
    # Written past save_version_guide, which now refuses this shape: the record under
    # test predates that rule and could not be authored today.
    stored = find_latest_review_guide("demo", version_id)
    assert stored is not None
    payload = stored.model_dump()
    for step in payload["steps"]:
        del step["data_description"]
    ReviewGuide.model_validate(payload).save()
    return version_id


def test_a_section_written_before_the_field_existed_still_loads(project_dir):
    """The case a REQUIRED field would orphan: the stored record predates the field."""
    version_id = _store_a_guide_written_before_the_field_existed(project_dir)

    view = build_run_guide_view("demo", _manifest(version_id))

    assert [s.data_description for s in view.steps] == [None, None]


def test_nothing_stands_in_for_a_missing_sentence(project_dir):
    version_id = _store_a_guide_written_before_the_field_existed(project_dir)

    view = build_run_guide_view("demo", _manifest(version_id))

    assert view.steps[0].data_description is None
    assert view.steps[0].title == "Read the rows"


# ── the step's own shape ─────────────────────────────────────────────────────

def _outputs(step) -> list[tuple[str, int | None]]:
    return [(s.stage_id, s.output_row_count) for s in step.outputs]


def test_a_step_leaves_the_stage_none_of_its_others_reads(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert _outputs(view.steps[1]) == [("attach_source", 4)]


def test_a_step_that_forks_leaves_every_branch_counted_on_its_own(project_dir):
    steps = [ReviewGuideStep(title="Both roots", prose="Two inputs that never meet.",
                             stage_ids=["load_rows", "load_sources"],
                             data_description="The filed rows, and the source list.")]
    version_id = _version_with_guide(project_dir, steps=steps, unnarrated=[
        "add_flag", "keep_flagged", "attach_source",
    ])

    view = build_run_guide_view("demo", _manifest(version_id))

    assert _outputs(view.steps[0]) == [("load_rows", 10), ("load_sources", 3)]


def test_a_stage_read_only_from_outside_the_step_is_still_a_terminal(project_dir):
    steps = [_STEPS[0], ReviewGuideStep(
        title="Cut it down", prose="Keeps the flagged rows.",
        stage_ids=["add_flag", "keep_flagged"],
        data_description="Only the rows carrying the flag.")]
    version_id = _version_with_guide(project_dir, steps=steps,
                                     unnarrated=["load_sources", "attach_source"])

    view = build_run_guide_view("demo", _manifest(version_id))

    assert _outputs(view.steps[1]) == [("keep_flagged", 4)]


def test_a_terminal_the_run_did_not_execute_stays_unknown(project_dir):
    manifest = _manifest(_version_with_guide(project_dir), executed=["add_flag"])
    view = build_run_guide_view("demo", manifest)

    assert _outputs(view.steps[1]) == [("attach_source", None)]


def test_a_step_of_input_stages_alone_still_reports_its_output(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert _outputs(view.steps[0]) == [("load_rows", 10)]

# ── list_written_columns on its own ──────────────────────────────────────────

def test_a_report_stage_producing_nothing_writes_no_columns():
    report = parse_stage({
        "id": "write_it", "description": "Write it", "type": "report",
        "inputs": [{"id": "attach_source"}],
        "report": {"format": "csv", "destination": "out/"},
        "signature": {"form": "replaces"},
        "function": {"kind": "inline",
                     "code": "def transform(df, output_dir):\n    return df\n"},
    })
    assert list_written_columns(
        WorkflowStage(stage=report, inputs=[], output_schema=None)) == []
