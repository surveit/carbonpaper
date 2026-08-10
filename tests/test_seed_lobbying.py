from __future__ import annotations

from pathlib import Path

from app.services import project, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data" / "lobbying_issue_triage.json"
_EXPECTED_STAGE_IDS = {"raw_filings", "classify_issues", "rank_by_spend", "publish_report"}


def test_committed_lobbying_fixture_imports_and_validates_cleanly(tmp_path):
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()

    wf = WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert {stage.id for stage in wf.stages} == _EXPECTED_STAGE_IDS

    imported_name = import_project(wf, name="lobbying_smoke")
    project_dir = examples_dir / imported_name
    assert imported_name in project.list_projects()

    summary = project.describe_workflow(imported_name)
    assert summary["issues"] == []
    assert {stage["id"] for stage in summary["stages"]} == _EXPECTED_STAGE_IDS

    # The strict loader too — the same one create_version required to succeed
    # before import_project's version snapshot was written.
    loaded_stages = load_workflow(project_dir)
    assert {stage.id for stage in loaded_stages} == _EXPECTED_STAGE_IDS

    versions = versioning.list_versions(project_dir)
    assert len(versions) == 1

    assert project.project_state(project_dir).workflow.n_stages == len(wf.stages)

    # The sample input CSV ships as a SIBLING fixture, not inside the
    # WorkflowFile and not auto-copied into the project (binding a file is a
    # run-time concern) — see app.seeds.__init__'s documented layout.
    sibling_csv = _FIXTURE_PATH.with_suffix(".csv")
    assert sibling_csv.is_file()
    assert not (project_dir / "input").exists()


def test_the_classify_stage_splits_its_prompt_into_a_fixed_prefix_and_a_per_row_template():
    wf = WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    classify_stage = next(stage for stage in wf.stages if stage.id == "classify_issues")

    assert classify_stage.llm is not None
    assert classify_stage.llm.prompt_instructions.strip() != ""

    data_template = classify_stage.llm.prompt_data_template
    for placeholder in ("{client}", "{registrant}", "{filing_period}", "{specific_issues}"):
        assert placeholder in data_template
