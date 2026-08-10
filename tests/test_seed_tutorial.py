from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from app.models import Stage, StageType
from app.models.errors import StepRefused
from app.models.review_guide import ReviewGuideDraft
from app.runtime.context import RunContext
from app.runtime.stages import HANDLERS
from app.services import project, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project
from app.tools.tutorial import _read_fixture_bound_to
from arch.test_no_html_in_python import find_html_tag_string_literals

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "seeds" / "data" / "tutorial_lobbying_triage.json"
)
_CSV_PATH = _FIXTURE_PATH.with_suffix(".csv")
_GUIDE_PATH = _FIXTURE_PATH.parent / "review_guides" / _FIXTURE_PATH.name
_TEMPLATE_PATH = _FIXTURE_PATH.parent / "tutorial_triage_report.html"
_TEMPLATE_TOKEN = "[[TEMPLATE_PATH]]"
# Long enough to say what to check, short enough that the check is what is read.
_GUIDE_PROSE_CEILING = 210

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
_BATCH_SIZE = 12
# The cap the tour's first run passes as limits {"raw_filings": N}.
_TOUR_LIMIT = 6

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


def test_classify_issues_reads_a_dozen_filings_per_model_call():
    classify = _stage(_load_fixture(), "classify_issues")
    # Each call spawns a process, so the batch size is the tour's wall clock.

    assert classify.llm is not None
    assert classify.llm.batch_size == _BATCH_SIZE
    assert _ROWS_KEPT <= _BATCH_SIZE * 2
    for placeholder in ("{client}", "{registrant}", "{filing_period}", "{specific_issues}"):
        assert placeholder in classify.llm.prompt_data_template


def test_the_methodology_document_states_the_batch_size_the_stage_uses():
    wf = _load_fixture()
    # The document is the source of record a reviewer reads against the stage.

    assert "twelve at a time in one model call" in wf.document
    assert "four at a time" not in wf.document


def test_the_first_six_filings_are_one_model_call():
    # What the tour's small run costs: beat 2 caps raw_filings at 6 rows.
    df = pd.read_csv(_CSV_PATH).head(_TOUR_LIMIT)

    assert int((df["amount_usd"] >= 50000).sum()) <= _BATCH_SIZE


def test_the_committed_review_guide_accounts_for_every_stage():
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    narrated = [stage_id for step in guide.steps for stage_id in step.stage_ids]
    assert narrated == _EXPECTED_STAGE_IDS
    assert guide.unnarrated == []
    for step in guide.steps:
        assert step.data_description and step.data_description.strip()


def test_the_review_guide_keeps_every_check_without_the_padding():
    """Each step is capped, and each capped step still names what a reviewer must check."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    over = [step.title for step in guide.steps if len(step.prose) > _GUIDE_PROSE_CEILING]
    assert not over, over
    prose = " ".join(step.prose for step in guide.steps)
    for check in (
        "Synthetic",
        "editorial choice",
        "the line you would draw",
        "the weakest link",
        "a wrong flag traces to a wrong classification",
        "Check that reason against the text",
    ):
        assert check in prose, check


# ── the published report ─────────────────────────────────────────────────────


def _publish_a_report(tmp_path, df: pd.DataFrame) -> str:
    stage = _stage(_bound_fixture(), "publish_report")
    ctx = RunContext.for_stages_outside_a_run(tmp_path, tmp_path)
    out = HANDLERS[StageType(stage.type)].execute(stage, {"flag_followup": df}, ctx)
    assert out is not None
    return Path(out.iloc[0]["report_path"]).read_text(encoding="utf-8")


def _bound_fixture() -> WorkflowFile:
    return _read_fixture_bound_to(_CSV_PATH, _TEMPLATE_PATH)


def _two_filings() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "filing_id": "F-1", "client": "Vague Client", "registrant": "Firm A",
                "amount_usd": 250000, "filing_period": "2024 Q1",
                "specific_issues": "General discussions regarding federal policy.",
                "policy_area": "Other", "targets_specific_bill": False,
                "primary_ask": "Wants favourable treatment, unspecified.",
                "needs_followup": True,
            },
            {
                "filing_id": "F-2", "client": "Specific Client", "registrant": "Firm B",
                "amount_usd": 60000, "filing_period": "2024 Q1",
                "specific_issues": "Support for H.R. 3684 highway provisions.",
                "policy_area": "Transportation", "targets_specific_bill": True,
                "primary_ask": "Wants the highway provisions kept.",
                "needs_followup": False,
            },
        ]
    )


def test_the_report_says_why_a_flagged_filing_needs_a_second_look(tmp_path):
    """A bare flag is not an argument: show the money, the stated ask, and what is missing."""
    page = _publish_a_report(tmp_path, _two_filings())

    assert "No specific bill named" in page
    assert "Catch-all Other policy area" in page
    assert "$250,000" in page
    assert "Wants favourable treatment, unspecified." in page
    assert "General discussions regarding federal policy." in page


def test_the_report_invents_no_reason_for_a_filing_that_was_not_flagged(tmp_path):
    page = _publish_a_report(tmp_path, _two_filings().tail(1))

    assert "No specific bill named" not in page
    assert "Catch-all Other policy area" not in page
    assert "1 filings cleared the spend filter; 0 are flagged" in page


def test_the_report_scores_nothing_and_recommends_nothing(tmp_path):
    page = _publish_a_report(tmp_path, _two_filings())

    assert "reading aid, not a verdict" in page
    for verdict_word in ("score", "rank", "recommend", "priority"):
        assert verdict_word not in page.lower(), verdict_word


def test_the_report_step_refuses_a_flag_it_cannot_account_for(tmp_path):
    """Flagged yet naming a bill and outside the catch-all: flag and data disagree."""
    df = _two_filings().tail(1).assign(needs_followup=True)

    with pytest.raises(StepRefused, match="cannot say why it was flagged"):
        _publish_a_report(tmp_path, df)


# ── the template is a file, not a string in the code ─────────────────────────


def test_the_publish_stage_carries_no_markup_of_its_own():
    """tests/arch/test_no_html_in_python.py's rule, applied to the stage's own code."""
    code = _stage(_load_fixture(), "publish_report").function.code
    assert code is not None

    assert find_html_tag_string_literals(ast.parse(code)) == []
    assert _TEMPLATE_TOKEN in code, "the code no longer names a template to read"


def test_seeding_binds_the_committed_template_into_the_publish_stage():
    bound = _stage(_bound_fixture(), "publish_report").function.code
    assert bound is not None

    assert _TEMPLATE_TOKEN not in bound
    assert _TEMPLATE_PATH.as_posix() in bound
    # A Windows path would land inside a Python string literal as escape sequences.
    assert "\\" not in _TEMPLATE_PATH.as_posix()


def test_a_missing_template_stops_the_seeding_rather_than_shipping_a_broken_stage(tmp_path):
    with pytest.raises(FileNotFoundError, match="tutorial fixture needs is missing"):
        _read_fixture_bound_to(_CSV_PATH, tmp_path / "gone.html")


def test_the_report_step_stops_when_the_template_loses_a_section(tmp_path):
    truncated = tmp_path / "truncated.html"
    kept = _TEMPLATE_PATH.read_text(encoding="utf-8").split("<!--@ reason -->")[0]
    truncated.write_text(kept, encoding="utf-8")
    stage = _stage(_read_fixture_bound_to(_CSV_PATH, truncated), "publish_report")
    ctx = RunContext.for_stages_outside_a_run(tmp_path, tmp_path)

    with pytest.raises(ValueError, match="has no"):
        HANDLERS[StageType(stage.type)].execute(
            stage, {"flag_followup": _two_filings()}, ctx
        )


def test_the_template_declares_every_section_the_step_reads():
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    code = _stage(_load_fixture(), "publish_report").function.code
    assert code is not None
    wanted = next(
        node.value
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "SECTIONS" for t in node.targets)
    )

    for section in ast.literal_eval(wanted):
        assert f"<!--@ {section} -->" in template, section


def test_the_fixture_is_committed_as_json_and_the_template_beside_it():
    assert _TEMPLATE_PATH.is_file()
    # The seed glob reads data/*.json as workflows; the template must not be one.
    assert json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))["name"]
    assert _TEMPLATE_PATH.suffix == ".html"
