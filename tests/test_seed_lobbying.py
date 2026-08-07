from __future__ import annotations

from pathlib import Path

from app.services import project, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data" / "lobbying_issue_triage.json"
_EXPECTED_STAGE_IDS = {"raw_filings", "classify_issues", "rank_by_spend", "publish_report"}


def test_committed_lobbying_fixture_imports_and_validates_cleanly(tmp_path):
    """WorkflowFile.model_validate_json + import_project on the committed
    fixture: the project lists, its workflow loads with the 4 documented
    stages, and exactly one (runnable) version exists. Approval coverage and
    the data-model state are entirely unreviewed post-import — review state is
    not part of a WorkflowFile, so this is never fabricated as "carried over"
    from whatever the source project had recorded. Never executes the
    workflow (no LLM call) — this is an import + validate smoke test."""
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


def test_seed_lobbying_issue_triage_loads():
    """The classify_issues llm_transform stage's prompt is split into a
    row-invariant prompt_instructions and a per-row prompt_data_template
    (the cacheable-prefix / per-row split): the fixed policy-area rubric
    lives in prompt_instructions with no per-row placeholders, while the
    filing-specific context and issue text — with the original {client},
    {registrant}, {filing_period}, {specific_issues} placeholders — live in
    prompt_data_template."""
    wf = WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    classify_stage = next(stage for stage in wf.stages if stage.id == "classify_issues")

    assert classify_stage.llm is not None
    assert classify_stage.llm.prompt_instructions.strip() != ""

    data_template = classify_stage.llm.prompt_data_template
    for placeholder in ("{client}", "{registrant}", "{filing_period}", "{specific_issues}"):
        assert placeholder in data_template
