"""What the run page reads off the workflow around a version's authored review guide. Project
scoping is by directory name under a repointed projects dir, isolated per test by
the autouse in-memory store (conftest.fresh_store)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.models import parse_stage
from app.models.review_guide import ReviewGuideStep
from app.services.versioning import ReviewGuide
from app.services import workspace
from app.services.run_guide import (
    build_run_guide_view,
    find_guideless_version_id,
    list_written_columns,
)
from app.services.versioning import (
    create_version_from_stages,
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

# load_rows → add_flag → keep_flagged ─┐
#                        load_sources ─┴→ attach_source
_STAGES: list[dict[str, Any]] = [
    {"id": "load_rows", "description": "Load rows", "type": "input_data",
     "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _ROWS["columns"]}},
    {"id": "load_sources", "description": "Load sources", "type": "input_data",
     "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _SOURCES["columns"]}},
    {"id": "add_flag", "description": "Flag rows", "type": "python_row_function",
     "inputs": [{"id": "load_rows", "schema": _ROWS}],
     "function": _ROW_FUNCTION, "signature": {
         "form": "extends",
         "reads": [{"input": "load_rows", "columns": _ROWS["columns"]}],
         "adds": [_FLAG],
     }},
    {"id": "keep_flagged", "description": "Keep the flagged rows", "type": "filter_rows",
     "inputs": [{"id": "add_flag", "schema": _FLAGGED}],
     "filter": {"code": "def should_include(row):\n    return row['flag']\n"},
     "signature": {"form": "extends"}},
    {"id": "attach_source", "description": "Attach the source", "type": "enrich",
     "inputs": [{"id": "keep_flagged", "schema": _FLAGGED},
                {"id": "load_sources", "schema": _SOURCES}],
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
                    stage_ids=["load_rows"]),
    ReviewGuideStep(title="Decide what counts",
                    prose="A row is kept on `flag`, then given its `source`.",
                    stage_ids=["attach_source", "add_flag"]),
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


def _manifest(
    version_id: str,
    *,
    executed: list[str] | None = None,
    counts: dict[str, int] | None = None,
) -> dict:
    ran = _STAGES if executed is None else [s for s in _STAGES if s["id"] in executed]
    measured = _COUNTS if counts is None else counts
    return {
        "workflow_version": version_id,
        "stage_records": [
            {"stage_id": s["id"], "status": "ok",
             "output_row_count": measured.get(s["id"])}
            for s in ran
        ],
    }


def _stages_by_id(view) -> dict:
    return {
        s.stage_id: s
        for s in [*view.steps[0].stages, *view.steps[1].stages, *view.unnarrated]
    }


def _version_with_guide(project_dir: Path, **overrides) -> str:
    version = create_version_from_stages(
        project_dir, _STAGES, message="v1", reviewer="ada"
    )
    guide = ReviewGuide(
        project=project_dir.name,
        version_id=version.version_id,
        steps=overrides.get("steps", _STEPS),
        unnarrated=overrides.get("unnarrated", _UNNARRATED),
    )
    save_version_guide(project_dir, version.version_id, guide)
    return version.version_id


# ── no guide, no panel ───────────────────────────────────────────────────────

def test_no_view_when_the_pinned_version_carries_no_guide(project_dir):
    version = create_version_from_stages(
        project_dir, _STAGES, message="v1", reviewer="ada"
    )
    assert build_run_guide_view("demo", _manifest(version.version_id)) is None


def test_no_view_when_the_manifest_records_no_version(project_dir):
    assert build_run_guide_view("demo", {"stage_records": []}) is None


def test_no_view_when_the_pinned_version_cannot_be_read(project_dir):
    _version_with_guide(project_dir)
    assert build_run_guide_view("demo", _manifest("20200101T000000")) is None


# ── the offer to write one ───────────────────────────────────────────────────

def test_a_guideless_pinned_version_is_named_so_a_guide_can_be_written_for_it(project_dir):
    version = create_version_from_stages(
        project_dir, _STAGES, message="v1", reviewer="ada"
    )
    assert find_guideless_version_id("demo", _manifest(version.version_id)) == version.version_id


def test_a_version_that_already_has_a_guide_is_not_offered_one(project_dir):
    version_id = _version_with_guide(project_dir)
    assert find_guideless_version_id("demo", _manifest(version_id)) is None


@pytest.mark.parametrize("manifest", [{"stage_records": []}, _manifest("20200101T000000")])
def test_an_unresolvable_version_is_not_offered_a_guide(project_dir, manifest):
    """A guide is stored ON a version, so no readable version means nothing to offer."""
    _version_with_guide(project_dir)
    assert find_guideless_version_id("demo", manifest) is None


# ── the facts read off the stages ────────────────────────────────────────────

def test_a_step_carries_its_authored_prose_and_title(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert [step.title for step in view.steps] == ["Read the rows", "Decide what counts"]
    assert view.steps[0].prose == "Reads every `doc_id` filed."


def test_a_steps_stages_come_back_in_execution_order_not_guide_order(project_dir):
    """The guide names attach_source before add_flag; the run reaches it after."""
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert [s.stage_id for s in view.steps[1].stages] == ["add_flag", "attach_source"]


def test_each_stage_carries_its_definition_from_the_pinned_version(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    [loaded] = view.steps[0].stages
    assert loaded.stage.description == "Load rows"
    assert loaded.stage.type == "input_data"


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
    """A document written past save_version_guide's validation must not lose a stage."""
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
    assert known.stage_id == "load_rows" and known.stage is not None
    assert unresolved.stage_id == "renamed_away"
    assert unresolved.stage is None
    assert unresolved.written_columns == []


# ── the row counts, measured off the run's own manifest ──────────────────────

def test_each_stage_carries_the_row_count_this_run_measured_for_it(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    counts = {s.stage_id: s.output_row_count for s in _stages_by_id(view).values()}
    assert counts == _COUNTS


def test_a_stage_the_run_has_no_record_for_has_an_unknown_count_not_zero(project_dir):
    manifest = _manifest(_version_with_guide(project_dir), executed=["load_rows"])
    view = build_run_guide_view("demo", manifest)

    absent = _stages_by_id(view)["add_flag"]
    assert absent.output_row_count is None
    assert absent.row_delta is None


def test_a_record_carrying_no_count_leaves_the_count_unknown(project_dir):
    """A stage that failed before it wrote a frame is recorded with no count at all."""
    manifest = _manifest(_version_with_guide(project_dir))
    manifest["stage_records"][1]["output_row_count"] = None

    view = build_run_guide_view("demo", manifest)

    assert _stages_by_id(view)["load_sources"].output_row_count is None


def test_a_measured_empty_frame_reads_as_zero_not_as_unknown(project_dir):
    counts = {**_COUNTS, "keep_flagged": 0, "attach_source": 0}
    manifest = _manifest(_version_with_guide(project_dir), counts=counts)

    view = build_run_guide_view("demo", manifest)

    emptied = _stages_by_id(view)["keep_flagged"]
    assert emptied.output_row_count == 0
    assert emptied.row_delta == -10


def test_a_pass_through_stage_measures_a_delta_of_zero(project_dir):
    """add_flag writes a column onto all 10 rows load_rows read, dropping none."""
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert _stages_by_id(view)["add_flag"].row_delta == 0


def test_a_row_dropping_stage_measures_the_rows_it_dropped(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert _stages_by_id(view)["keep_flagged"].row_delta == -6


def test_a_stage_with_no_input_has_no_delta_rather_than_a_zero_one(project_dir):
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert _stages_by_id(view)["load_rows"].row_delta is None


def test_a_delta_against_an_unmeasured_input_is_unknown(project_dir):
    manifest = _manifest(_version_with_guide(project_dir), executed=["add_flag"])
    view = build_run_guide_view("demo", manifest)

    flagged = _stages_by_id(view)["add_flag"]
    assert flagged.output_row_count == 10
    assert flagged.row_delta is None


def test_a_delta_is_taken_against_the_first_input_of_a_join(project_dir):
    """attach_source joins load_sources INTO keep_flagged, so keep_flagged is the base."""
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert _stages_by_id(view)["attach_source"].row_delta == 0


# ── the step's own shape ─────────────────────────────────────────────────────

def test_a_step_leaves_the_count_its_stages_meet_at(project_dir):
    """add_flag feeds attach_source through keep_flagged, so the step ends at its 4."""
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert view.steps[1].output_row_count == 4


def test_a_step_whose_stages_do_not_meet_states_no_count(project_dir):
    steps = [ReviewGuideStep(title="Both roots", prose="Two inputs that never meet.",
                             stage_ids=["load_rows", "load_sources"])]
    version_id = _version_with_guide(project_dir, steps=steps, unnarrated=[
        "add_flag", "keep_flagged", "attach_source",
    ])

    view = build_run_guide_view("demo", _manifest(version_id))

    assert view.steps[0].output_row_count is None


def test_a_step_reaching_a_stage_the_run_did_not_execute_leaves_no_count(project_dir):
    manifest = _manifest(_version_with_guide(project_dir), executed=["add_flag"])
    view = build_run_guide_view("demo", manifest)

    assert view.steps[1].output_row_count is None


def test_a_step_holding_a_row_dropping_stage_is_marked_as_changing_the_row_set(project_dir):
    steps = [_STEPS[0], ReviewGuideStep(
        title="Cut it down", prose="Keeps the flagged rows.",
        stage_ids=["add_flag", "keep_flagged"])]
    version_id = _version_with_guide(project_dir, steps=steps,
                                     unnarrated=["load_sources", "attach_source"])

    view = build_run_guide_view("demo", _manifest(version_id))

    assert view.steps[1].changes_row_set is True
    assert view.steps[1].passes_rows_through is False


def test_a_step_that_only_widened_its_rows_says_so(project_dir):
    steps = [_STEPS[0], ReviewGuideStep(
        title="Widen", prose="Writes `flag` onto every row.", stage_ids=["add_flag"])]
    version_id = _version_with_guide(project_dir, steps=steps, unnarrated=[
        "load_sources", "keep_flagged", "attach_source",
    ])

    view = build_run_guide_view("demo", _manifest(version_id))

    assert view.steps[1].passes_rows_through is True
    assert view.steps[1].changes_row_set is False


def test_a_step_of_inputs_alone_claims_neither_shape(project_dir):
    """An input_data stage has no input to compare against, so nothing is claimed."""
    view = build_run_guide_view("demo", _manifest(_version_with_guide(project_dir)))

    assert view.steps[0].changes_row_set is False
    assert view.steps[0].passes_rows_through is False
    assert view.steps[0].output_row_count == 10


# ── list_written_columns on its own ──────────────────────────────────────────

def test_a_publish_stage_producing_nothing_writes_no_columns():
    publish = parse_stage({
        "id": "write_it", "description": "Write it", "type": "publish",
        "inputs": [{"id": "attach_source", "schema": _ATTACHED}],
        "publish": {"format": "csv", "destination": "out/"},
        "signature": {"form": "replaces"},
        "function": {"kind": "inline",
                     "code": "def transform(df, output_dir):\n    return df\n"},
    })
    assert list_written_columns(publish) == []
