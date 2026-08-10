from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models import Stage, StageType
from app.runtime.context import RunContext
from app.runtime.stages import HANDLERS
from app.services import project, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "seeds" / "data" / "tutorial_lobbying_triage.json"
)
_CSV_PATH = _FIXTURE_PATH.with_suffix(".csv")

_EXPECTED_STAGE_IDS = [
    "raw_filings",
    "significant_filings",
    "classify_issues",
    "flag_followup",
    "publish_report",
]

# Counted off the committed CSV: 24 filings, 6 of them reporting under $50,000.
_ROWS_IN_CSV = 24
_ROWS_BELOW_THRESHOLD = 6
_ROWS_KEPT = _ROWS_IN_CSV - _ROWS_BELOW_THRESHOLD

_POLICY_AREAS = [
    "Health",
    "Energy & Environment",
    "Finance & Taxation",
    "Technology",
    "Defense",
    "Transportation",
    "Agriculture",
    "Other",
]


def _load_fixture() -> WorkflowFile:
    return WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _stage(wf: WorkflowFile, stage_id: str) -> Stage:
    return next(stage for stage in wf.stages if stage.id == stage_id)


def _execute(stage: Stage, inputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    # The same ephemeral, run-dir-less context authored stage tests execute under.
    ctx = RunContext.for_stages_outside_a_run(None, None)
    result = HANDLERS[StageType(stage.type)].execute(stage, inputs, ctx)
    assert result is not None
    return result


def _stand_in_for_the_classifier(df: pd.DataFrame) -> pd.DataFrame:
    # Fills what classify_issues would add. Never calls a model.
    classified = df.copy()
    classified["policy_area"] = [
        _POLICY_AREAS[i % len(_POLICY_AREAS)] for i in range(len(classified))
    ]
    classified["targets_specific_bill"] = [i % 2 == 0 for i in range(len(classified))]
    classified["primary_ask"] = [
        f"stand-in ask for {filing_id}" for filing_id in classified["filing_id"]
    ]
    return classified


def test_committed_tutorial_fixture_imports_and_validates_cleanly(tmp_path):
    wf = _load_fixture()
    assert [stage.id for stage in wf.stages] == _EXPECTED_STAGE_IDS

    imported_name = import_project(wf, name="tutorial_smoke")
    assert imported_name in project.list_projects()

    summary = project.describe_workflow(imported_name)
    assert summary["issues"] == []
    assert [stage["id"] for stage in summary["stages"]] == _EXPECTED_STAGE_IDS

    project_dir = tmp_path / "examples" / imported_name
    loaded_stages = load_workflow(project_dir)
    assert [stage.id for stage in loaded_stages] == _EXPECTED_STAGE_IDS
    assert len(versioning.list_versions(project_dir)) == 1

    # The tutorial skips the data-model step, so the fixture carries no schemas.
    assert wf.data_model.schemas == []

    sibling_csv = _FIXTURE_PATH.with_suffix(".csv")
    assert sibling_csv.is_file()
    assert not (project_dir / "input").exists()


def test_the_bundled_csv_has_the_row_counts_the_filter_is_written_against():
    df = pd.read_csv(_CSV_PATH)

    assert list(df.columns) == [
        "filing_id", "client", "registrant", "amount_usd", "filing_period", "specific_issues",
    ]
    assert len(df) == _ROWS_IN_CSV
    assert int((df["amount_usd"] < 50000).sum()) == _ROWS_BELOW_THRESHOLD


def test_significant_filings_drops_the_filings_under_the_threshold():
    stage = _stage(_load_fixture(), "significant_filings")
    df = pd.read_csv(_CSV_PATH)

    kept = _execute(stage, {"raw_filings": df})

    assert len(df) == _ROWS_IN_CSV
    assert len(kept) == _ROWS_KEPT
    assert int(kept["amount_usd"].min()) == 50000


def test_flag_followup_is_grain_preserving():
    stage = _stage(_load_fixture(), "flag_followup")
    classified = _stand_in_for_the_classifier(
        pd.read_csv(_CSV_PATH).query("amount_usd >= 50000").reset_index(drop=True)
    )

    flagged = _execute(stage, {"classify_issues": classified})

    assert len(flagged) == len(classified) == _ROWS_KEPT
    assert list(flagged["filing_id"]) == list(classified["filing_id"])
    assert flagged["needs_followup"].notna().all()
    assert set(flagged["needs_followup"].map(type)) == {bool}


def test_flag_followup_flags_only_what_the_methodology_says_it_flags():
    stage = _stage(_load_fixture(), "flag_followup")
    classified = _stand_in_for_the_classifier(
        pd.read_csv(_CSV_PATH).query("amount_usd >= 50000").reset_index(drop=True)
    )

    flagged = _execute(stage, {"classify_issues": classified})

    expected = [
        (not targets_bill) or area == "Other"
        for targets_bill, area in zip(
            classified["targets_specific_bill"], classified["policy_area"]
        )
    ]
    assert list(flagged["needs_followup"]) == expected


def test_classify_issues_reads_four_filings_per_model_call():
    classify = _stage(_load_fixture(), "classify_issues")

    assert classify.llm is not None
    assert classify.llm.batch_size == 4
    for placeholder in ("{client}", "{registrant}", "{filing_period}", "{specific_issues}"):
        assert placeholder in classify.llm.prompt_data_template
