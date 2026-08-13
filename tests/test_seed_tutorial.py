from __future__ import annotations

import json
import re

from pathlib import Path

import pandas as pd
import pytest

from app.evals.compatibility import validate_eval_compatibility
from app.evals.dataset_columns import get_injected_columns
from app.evals.store import load_eval_config
from app.core.stage_cache import StageCacheEntry
from app.models import Stage, StageType
from app.models.errors import StepRefused
from app.models.review_guide import ReviewGuideDraft
from app.models.stages.human_review_queue import HumanReviewQueueStage
from app.runtime.context import RunContext, RunIdentity
from app.runtime.errors import HaltForReview
from app.runtime.stage_tests import run_stage_tests
from app.runtime.stages import HANDLERS
from app.services import project, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project
from app.tools.tutorial import (
    TutorialContext,
    read_seed_eval_config,
    seed_tutorial_project,
)
from conftest import as_inputs, make_run_context, pinned_stages, place_stage, rows_of

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "seeds" / "data" / "tutorial_say_versus_do.json"
)
_CSV_PATH = _FIXTURE_PATH.parent / "tutorial_lobbying_records.csv"
_COMMITMENTS_PATH = _FIXTURE_PATH.parent / "tutorial_public_commitments.csv"
_SOURCES_PATH = _FIXTURE_PATH.parent / "sources" / _FIXTURE_PATH.name
_GUIDE_PATH = _FIXTURE_PATH.parent / "review_guides" / _FIXTURE_PATH.name
# Shorter than the model's 255: a step nobody skims is a step nobody reads.
_GUIDE_PROSE_CEILING = 200

_EXPECTED_STAGE_IDS = [
    "raw_filings",
    "public_commitments",
    "check_filings",
    "matched_commitments",
    "judge_alignment",
    "review_contradictions",
    "publish_report",
]

_RECORD_COLUMNS = [
    "record_id", "organisation", "period", "lobbying_issue", "lobbying_quote",
    "lobbying_source_url", "lobbying_source_date",
]
_COMMITMENT_COLUMNS = [
    "organisation", "public_commitment", "commitment_quote", "commitment_source_url",
    "commitment_source_date",
]
# Counted off the committed CSVs. Every organisation carries a commitment, so the
# sample exercises no non-match — an organisation with none would be one nobody sourced.
_ROWS_IN_CSV = 6
_COMMITMENT_ROWS = 6
_BATCH_SIZE = 12
# The cap the tour's first run passes as limits {"raw_filings": N}.
_TOUR_LIMIT = 6
# Authored on check_filings, the one stage of this workflow that may carry them.
_SEEDED_EXAMPLES = 6

_CONTRADICTS = "Contradicts"
_NO_COMMITMENT = "No commitment given"
_JUDGMENT_VALUES = ["Contradicts", "Matches", "Unclear", _NO_COMMITMENT]

_REVIEW_STAGE = "review_contradictions"
_REVIEWED_COLUMN = "reviewed_judgment"
# The three verdicts the review runtime writes, as the fixture declares them.
_APPROVE, _MODIFY, _SKIPPED = "approve", "modify", "skipped"
# Stands in for a decision the run records: a name and the moment it was recorded.
_REVIEWER = "R. Vasquez"
_REVIEWED_AT = "2024-05-06T11:20:00"

_EVAL_PATH = _FIXTURE_PATH.parent / "evals" / _FIXTURE_PATH.name
_EVAL_ID = "hard_judgment_cases"
_OVERRIDE_STAGE = "matched_commitments"
_TARGET_STAGE = "judge_alignment"
_JUDGED_COLUMN = "ai_judgment"
# Counted off the committed eval dataset.
_EVAL_ROWS = 6
_BASE_URL = "http://127.0.0.1:8788/"


def _load_fixture() -> WorkflowFile:
    return WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _stage(wf: WorkflowFile, stage_id: str) -> Stage:
    return next(stage for stage in wf.stages if stage.id == stage_id)


def _execute(stage: Stage, inputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    # The same ephemeral, run-dir-less context authored stage tests execute under.
    ctx = RunContext.for_stages_outside_a_run(None)
    result = HANDLERS[StageType(stage.type)].execute(
        place_stage(stage), as_inputs(inputs), ctx)
    assert result is not None
    return rows_of(result)


def _all_records() -> pd.DataFrame:
    return pd.read_csv(_CSV_PATH)


def _checked(records: pd.DataFrame | None = None) -> pd.DataFrame:
    return _execute(
        _stage(_load_fixture(), "check_filings"),
        {"raw_filings": _all_records() if records is None else records},
    )


def _matched() -> pd.DataFrame:
    return _execute(
        _stage(_load_fixture(), "matched_commitments"),
        {
            "check_filings": _checked(),
            "public_commitments": pd.read_csv(_COMMITMENTS_PATH),
        },
    )


def _stand_in_for_the_model(df: pd.DataFrame) -> pd.DataFrame:
    # Fills what judge_alignment would add. Never calls a model.
    judged = df.copy()
    judged[_JUDGED_COLUMN] = [
        _NO_COMMITMENT if pd.isna(said) else _JUDGMENT_VALUES[i % 3]
        for i, said in enumerate(judged["public_commitment"])
    ]
    judged["ai_justification"] = [
        f"stand-in sentence for {record_id}" for record_id in judged["record_id"]
    ]
    return judged


def test_committed_tutorial_fixture_imports_and_validates_cleanly(tmp_path):
    wf = _load_fixture()
    assert [stage.id for stage in wf.stages] == _EXPECTED_STAGE_IDS

    imported_name = import_project(wf, name="tutorial_smoke")
    assert imported_name in project.list_projects()

    summary = project.read_workflow_summary(imported_name)
    assert summary.issues == []
    assert [stage.id for stage in summary.stages] == _EXPECTED_STAGE_IDS

    project_dir = tmp_path / "examples" / imported_name
    loaded_stages = load_workflow(project_dir.name)
    assert [stage.id for stage in loaded_stages] == _EXPECTED_STAGE_IDS
    assert len(versioning.list_versions(project_dir.name)) == 1

    # The tutorial skips the data-model step, so the fixture carries no schemas.
    assert wf.data_model.schemas == []

    assert _CSV_PATH.is_file() and _COMMITMENTS_PATH.is_file()
    assert not (project_dir / "input").exists()


def test_the_bundled_records_csv_carries_the_row_count_the_tour_reports():
    df = pd.read_csv(_CSV_PATH)

    assert list(df.columns) == _RECORD_COLUMNS
    assert len(df) == _ROWS_IN_CSV


def test_the_bundled_commitments_csv_is_one_row_per_organisation():
    """What lets the match be an enrich: a repeated organisation would fail the run."""
    df = pd.read_csv(_COMMITMENTS_PATH)

    assert list(df.columns) == _COMMITMENT_COLUMNS
    assert len(df) == _COMMITMENT_ROWS
    assert df["organisation"].is_unique


def test_no_row_carries_a_lobbying_firm_a_dollar_figure_or_a_filing_id():
    """The sources record none of the three, so a column for one could only be invented."""
    retired = {"registrant", "amount_usd", "filing_id", "filing_period", "client",
               "specific_issues", "alignment", "alignment_note", "reviewed_alignment"}

    for path in (_CSV_PATH, _COMMITMENTS_PATH):
        assert not retired & set(pd.read_csv(path).columns), path.name


def test_every_shipped_row_names_a_source_a_reader_can_open():
    """The claim this sample makes about itself: each half was read off a published page."""
    records = _all_records()
    commitments = pd.read_csv(_COMMITMENTS_PATH)

    assert records["lobbying_source_url"].str.startswith("https://").all()
    assert commitments["commitment_source_url"].str.startswith("https://").all()
    assert records["lobbying_source_date"].str.strip().str.len().min() > 0
    assert commitments["commitment_source_date"].str.strip().str.len().min() > 0


def test_the_source_date_records_what_the_page_says_rather_than_a_date_nobody_published():
    """Why the column is text: some of these pages carry no publication date at all."""
    dates = list(_all_records()["lobbying_source_date"]) + list(
        pd.read_csv(_COMMITMENTS_PATH)["commitment_source_date"])
    undated = [text for text in dates if not text[:1].isdigit()]

    assert undated, "no undated source left in the sample; the text column has no job"
    assert all("fetched" in text for text in undated), undated


def test_every_shipped_row_has_its_provenance_beside_it():
    """The raw beside the cooked: any claim on the report retraces to a quote and a link."""
    sources = json.loads(_SOURCES_PATH.read_text(encoding="utf-8"))
    records = _all_records()
    by_id = {entry["record_id"]: entry for entry in sources["records"]}

    assert list(by_id) == list(records["record_id"])
    for _, row in records.iterrows():
        entry = by_id[row["record_id"]]
        assert entry["lobbying_source_url"] == row["lobbying_source_url"]
        assert entry["lobbying_quote"] and entry["commitment_quote"]
        assert entry["label_reasoning"] and entry["confidence"]
        assert entry["researcher_label"] in _JUDGMENT_VALUES


def test_the_provenance_file_speaks_for_the_shipped_rows_and_no_others():
    """A row nobody shipped has no business carrying an allegation on this page."""
    sources = json.loads(_SOURCES_PATH.read_text(encoding="utf-8"))

    assert len(sources["records"]) == _ROWS_IN_CSV
    assert set(sources["records"][0]) == {
        "record_id", "organisation", "subject", "period",
        "commitment_quote", "commitment_source_url", "commitment_source_date",
        "commitment_source_type", "lobbying_quote", "lobbying_source_url",
        "lobbying_source_date", "lobbying_source_type",
        "researcher_label", "label_reasoning", "confidence",
    }


def test_the_sample_is_not_answerable_from_the_labels_alone():
    """The point of these six: a reader cannot sort them by tone of voice."""
    labels = _researcher_labels()

    assert sorted(set(labels.values())) == ["Contradicts", "Matches", "Unclear"]
    assert sum(1 for label in labels.values() if label == _CONTRADICTS) == 2
    # Two of the six are contradictions, so the tour's queue holds a pair to work.
    assert len(labels) == _ROWS_IN_CSV


def _researcher_labels() -> dict[str, str]:
    """Off the eval dataset, which is where the researcher's answer is committed."""
    dataset = _eval_dataset_file()
    return dict(zip(dataset["record_id"], dataset[_JUDGED_COLUMN]))


def test_the_whole_sample_fits_the_tours_capped_run():
    """Beat 2 caps raw_filings at 6 rows, and the sample is six."""
    assert len(_all_records()) <= _TOUR_LIMIT


# ── putting each promise beside the ask ──────────────────────────────────────


def test_the_match_is_many_to_one_and_drops_no_record():
    matched = _matched()
    records = _checked()

    assert len(matched) == len(records) == _ROWS_IN_CSV
    assert list(matched["record_id"]) == list(records["record_id"])
    # Every subject column flows through untouched; the step only ever ADDS.
    for column in records.columns:
        assert list(matched[column]) == list(records[column])
    assert list(matched.columns)[-4:] == [
        "public_commitment", "commitment_quote", "commitment_source_url",
        "commitment_source_date",
    ]


def test_every_record_lands_the_commitment_it_is_judged_against():
    matched = _matched()

    assert matched["public_commitment"].notna().all()
    assert matched["commitment_source_url"].notna().all()


def test_a_record_whose_organisation_has_no_commitment_survives_with_a_blank():
    """The non-match record: the record's own fields are all still there."""
    records = _all_records()
    records.loc[0, "organisation"] = "An Organisation Nobody Sourced"

    matched = _execute(
        _stage(_load_fixture(), "matched_commitments"),
        {"check_filings": _checked(records),
         "public_commitments": pd.read_csv(_COMMITMENTS_PATH)},
    )
    unmatched = matched[matched["public_commitment"].isna()]

    assert len(unmatched) == 1
    assert unmatched["commitment_source_url"].isna().all()
    for column in ("record_id", "organisation", "lobbying_issue", "lobbying_source_url"):
        assert unmatched[column].notna().all()


def test_a_repeated_commitment_row_fails_the_run_rather_than_multiplying_records():
    stage = _stage(_load_fixture(), "matched_commitments")
    commitments = pd.read_csv(_COMMITMENTS_PATH)
    doubled = pd.concat([commitments, commitments.head(1)], ignore_index=True)

    with pytest.raises(ValueError, match="public_commitments"):
        _execute(stage, {"check_filings": _checked(), "public_commitments": doubled})


# ── the check ────────────────────────────────────────────────────────────────


def test_every_committed_record_carries_a_source_the_check_accepts():
    """If one did not the tour's own run would stop, which is not the demo."""
    checked = _checked()
    records = _all_records()

    assert list(checked["lobbying_source_url"]) == [
        text.strip() for text in records["lobbying_source_url"]
    ]


def test_check_filings_is_grain_preserving_and_touches_nothing_else():
    checked = _checked()
    records = _all_records()

    assert len(checked) == len(records) == _ROWS_IN_CSV
    assert list(checked.columns) == list(records.columns)
    for column in records.columns:
        if column != "lobbying_source_url":
            assert list(checked[column]) == list(records[column])


def test_the_seeded_examples_pass_before_anything_has_been_run():
    """Beat 3 sends the reader to this panel; a failing case there is the tour's first impression."""
    report = run_stage_tests(list(_load_fixture().stages))

    assert report.summary.stages_run == 1
    assert report.summary.tests_total == _SEEDED_EXAMPLES
    assert report.count_failing_by_stage() == {}
    # Every stage that could carry examples does.
    assert report.untested_stages == []


def test_every_seeded_example_is_one_of_the_bundled_records():
    """An invented row reads as fabrication; each case names a record in the committed CSV."""
    tests = _stage(_load_fixture(), "check_filings").tests or []
    record_ids = set(_all_records()["record_id"])

    assert len(tests) == _SEEDED_EXAMPLES
    for test in tests:
        row = test.inputs["raw_filings"][0]
        assert row["record_id"] in record_ids, test.name


@pytest.mark.parametrize(
    "link", ["", "   ", "press release, on file", "www.example.org/statement",
             "ftp://example.org/statement.pdf"],
)
def test_a_source_a_reader_cannot_open_stops_the_run(link):
    """The cardinal case: an unopenable claim never reaches a page that invites checking."""
    records = _all_records()
    records.loc[0, "lobbying_source_url"] = link

    with pytest.raises(StepRefused, match=records.loc[0, "record_id"]):
        _checked(records)


def test_a_record_with_no_account_of_its_ask_stops_the_run():
    records = _all_records()
    records.loc[0, "lobbying_issue"] = "   "

    with pytest.raises(StepRefused, match="no ask to weigh"):
        _checked(records)


def test_a_record_that_does_not_say_when_its_source_was_read_stops_the_run():
    """An undated page is fine, as long as the record says that is what it is."""
    records = _all_records()
    records.loc[0, "lobbying_source_date"] = ""

    with pytest.raises(StepRefused, match="how old the ask is"):
        _checked(records)


# ── the model step ───────────────────────────────────────────────────────────


def test_judge_alignment_reads_the_whole_sample_in_one_model_call():
    judge = _stage(_load_fixture(), "judge_alignment")
    # Each call spawns a process, so the batch size is the tour's wall clock.

    assert judge.llm is not None
    assert judge.llm.batch_size == _BATCH_SIZE
    assert _ROWS_IN_CSV <= _BATCH_SIZE
    for placeholder in ("{organisation}", "{public_commitment}", "{commitment_quote}",
                        "{lobbying_issue}", "{lobbying_quote}"):
        assert placeholder in judge.llm.prompt_data_template


def test_the_model_is_shown_both_texts_and_told_to_use_nothing_else():
    """The organisations are real, which makes the instruction stricter, not looser."""
    judge = _stage(_load_fixture(), "judge_alignment")
    assert judge.llm is not None
    instructions = judge.llm.prompt_instructions

    assert "judge ONLY the two texts printed below" in instructions
    assert "cannot be checked by the person who reads your answer" in instructions
    assert "never invent, complete or paraphrase a commitment" in instructions.lower()
    # The match leaves this blank for an unmatched record, and pandas renders it `nan`.
    assert _NO_COMMITMENT in instructions and "`nan`" in instructions


def test_the_methodology_document_states_the_batch_size_the_stage_uses():
    wf = _load_fixture()
    # The document is the source of record a reviewer reads against the stage.

    # "up to": the last call of a run holds whatever is left over.
    assert "read up to twelve at a time in one model call" in wf.document
    # And what this sample actually costs, which is one call, not twelve.
    assert "All six of these travel in a single call." in wf.document
    # And what the saving costs, which the document owes a reader of the labels.
    assert "swayed by the ones it happens to travel with" in wf.document


def test_the_methodology_document_says_what_the_data_is_and_where_it_came_from():
    wf = _load_fixture()

    assert "The six records here are real" in wf.document
    assert "app/seeds/data/sources/tutorial_say_versus_do.json" in wf.document
    # And what it does NOT carry, which is the half the old invented sample made up.
    assert "no lobbying firm, and\nno money" in wf.document
    assert "INVENTED" not in wf.document


def test_the_methodology_document_keeps_a_section_for_every_stage():
    wf = _load_fixture()
    sections = {
        stage.source.section for stage in wf.stages
        if stage.source is not None and stage.source.section
    }

    for section in sections:
        assert f"## {section}" in wf.document, section


# ── the review guide ─────────────────────────────────────────────────────────


def test_the_committed_review_guide_accounts_for_every_stage():
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    narrated = [stage_id for step in guide.steps for stage_id in step.stage_ids]
    assert narrated == _EXPECTED_STAGE_IDS
    assert guide.unnarrated == []
    for step in guide.steps:
        assert step.data_description and step.data_description.strip()


def test_the_committed_review_guide_says_what_the_reader_is_here_to_decide():
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    assert guide.goal is not None
    # The judgement, in the second person — not an account of what the workflow does.
    assert guide.goal.startswith("You're here to")
    assert "publicly committed" in guide.goal and "contradicts" in guide.goal


def test_the_review_guide_keeps_every_check_without_the_padding():
    """Each step is capped, and each capped step still names what a reviewer must check."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    over = [step.title for step in guide.steps if len(step.prose) > _GUIDE_PROSE_CEILING]
    assert not over, over
    prose = " ".join(step.prose for step in guide.steps)
    for check in (
        "a repeat stops the run",
        "the absence IS the record",
        "Trust this step least",
        "stops the run here",
        # The guide ends where the run does: on the file it published.
        "open it under Published",
    ):
        assert check in prose, check


def test_the_review_guide_speaks_no_jargon():
    """A journalist reads this rail. Every word in it has to be one they already use."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )
    prose = " ".join([*(step.prose for step in guide.steps), guide.goal or ""]).lower()

    for word in ("join", "merge", "key", "deduplicate", "schema", "grain", "enrich",
                 "column", "null", "cast", "parse", "lineage", "upstream",
                 "downstream", "row per", "row-level"):
        assert word not in prose, word


# A number in front of a countable noun ("six real records", "3 rows"): the run measures
# that and prints it on the data link, so prose stating it is a second number that can
# disagree. `one` is left out — "one row per record" is the grain, not a count.
_COUNTED_NOUN = (
    r"records?|rows?|stages?|steps?|filings?|organisations?"
    r"|commitments?|promises?|columns?|entries|entry|results?"
)
_STATED_COUNT = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|\d[\d,]*)\b"
    rf"(?:\s+\w+){{0,2}}\s+\b(?:{_COUNTED_NOUN})\b",
    re.IGNORECASE,
)
_SCREEN_POSITION = re.compile(
    r"\b(?:above|below|underneath|further down|further up|on the left|on the right"
    r"|at the top|at the bottom|in the rail|in the sidebar)\b",
    re.IGNORECASE,
)


def test_the_review_guide_states_no_count_and_names_no_place_on_the_screen():
    """The run measures the count and the layout moves: prose saying either goes stale silently."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    offenders = _find_counts_and_screen_positions(guide)

    assert offenders == [], offenders


def _find_counts_and_screen_positions(guide: ReviewGuideDraft) -> list[str]:
    return [
        f"{step.title} / {field}: {hit!r}"
        for step in guide.steps
        for field, text in (
            ("title", step.title),
            ("prose", step.prose),
            ("data_description", step.data_description or ""),
        )
        for pattern in (_STATED_COUNT, _SCREEN_POSITION)
        for hit in pattern.findall(text)
    ]


# ── the seeded eval ──────────────────────────────────────────────────────────


def _import_and_pin(tmp_path):
    name = import_project(_load_fixture(), name="tutorial_smoke")
    workflow, _version = pinned_stages(tmp_path / "examples" / name)
    return name, workflow


def _eval_dataset_file() -> pd.DataFrame:
    """Read off the committed path, for the tests that run outside a workspace."""
    config = json.loads(_EVAL_PATH.read_text(encoding="utf-8"))
    return pd.read_csv(Path(__file__).resolve().parents[1] / config["table"]["path"])


def _eval_dataset(name: str = "tutorial_smoke") -> pd.DataFrame:
    config = read_seed_eval_config(name)
    assert config.table is not None
    return pd.read_csv(Path(__file__).resolve().parents[1] / config.table.path)


def test_the_committed_eval_still_fits_the_tutorial_workflow(tmp_path):
    name, workflow = _import_and_pin(tmp_path)

    report = validate_eval_compatibility(read_seed_eval_config(name), workflow)

    assert report.ok, report.problems
    assert report.settings is not None
    # The dataset stands in for everything upstream, so one model step re-runs.
    assert report.settings.frontier == [_TARGET_STAGE]


def test_the_eval_dataset_supplies_every_column_the_override_stage_emits(tmp_path):
    _name, workflow = _import_and_pin(tmp_path)
    by_id = workflow.index_workflow_stages_by_id()

    injected = get_injected_columns(
        by_id[_OVERRIDE_STAGE], by_id[_TARGET_STAGE], [_JUDGED_COLUMN])

    assert list(_eval_dataset().columns) == [c.name for c in injected] + [_JUDGED_COLUMN]


def test_the_eval_labels_only_with_verdicts_the_stage_may_emit():
    judge = _stage(_load_fixture(), _TARGET_STAGE)
    column = next(c for c in judge.signature.adds if c.name == _JUDGED_COLUMN)
    dataset = _eval_dataset_file()

    assert len(dataset) == _EVAL_ROWS
    assert set(dataset[_JUDGED_COLUMN]) <= set(column.enum or [])
    # No record here lacks a commitment, so the absence label is not among them: an
    # organisation with no sourced commitment would be one nobody could have researched.
    assert _NO_COMMITMENT not in set(dataset[_JUDGED_COLUMN])


def test_the_eval_scores_the_rows_the_workflow_actually_ships():
    """Its answer key is the researcher's, on the same six records — not a second sample."""
    dataset = _eval_dataset_file()

    assert list(dataset["record_id"]) == list(_all_records()["record_id"])
    assert list(dataset["lobbying_issue"]) == list(_all_records()["lobbying_issue"])


def test_the_eval_says_whose_reading_its_answer_key_is():
    """A label presented as ground truth is a claim nobody can appeal."""
    config = json.loads(_EVAL_PATH.read_text(encoding="utf-8"))

    assert "one researcher's reading, not ground truth" in config["description"]
    assert "sources/tutorial_say_versus_do.json" in config["description"]


def test_scoring_the_eval_costs_one_model_call():
    """What a reader re-running it pays: the target reads the dataset at its own batch size."""
    judge = _stage(_load_fixture(), _TARGET_STAGE)
    assert judge.llm is not None

    assert len(_eval_dataset_file()) <= judge.llm.batch_size


def test_the_tour_seeds_the_eval_beside_the_review_guide(projects_root):
    seeded = seed_tutorial_project(TutorialContext(base_url=_BASE_URL))

    stored = load_eval_config(seeded.project.id, _EVAL_ID)

    assert stored.project == seeded.project.id
    assert (stored.override_stage, stored.target_stage) == (_OVERRIDE_STAGE, _TARGET_STAGE)
    assert [check.output_column for check in stored.expected_outputs] == [_JUDGED_COLUMN]
    # run_eval takes the id, so the tour is handed it rather than slicing it off a URL.
    assert seeded.eval_id == _EVAL_ID


def test_the_committed_eval_names_no_project_of_its_own():
    """The project id is minted at import, so a project written here would be a guess."""
    assert "project" not in json.loads(_EVAL_PATH.read_text(encoding="utf-8"))


# ── the review step ──────────────────────────────────────────────────────────


def _review_stage() -> HumanReviewQueueStage:
    stage = _stage(_load_fixture(), _REVIEW_STAGE)
    assert isinstance(stage, HumanReviewQueueStage)
    return stage


def test_only_the_records_the_model_called_contradictions_reach_a_reviewer():
    """The other three labels are published as nobody's finding, so nobody is asked."""
    queue = _review_stage().queue

    assert queue.filter == f"{_JUDGED_COLUMN} == '{_CONTRADICTS}'"
    assert queue.reviewed_columns == {_JUDGED_COLUMN: _REVIEWED_COLUMN}


def test_the_reviewers_label_lands_beside_the_models_and_never_on_it():
    stage = _review_stage()
    added = {column.name: column for column in stage.signature.adds}

    assert stage.signature.rewrites == []
    assert added[_REVIEWED_COLUMN].enum == _JUDGMENT_VALUES
    # Non-nullable: every record reaches the report carrying a label, reviewed or not.
    assert added[_REVIEWED_COLUMN].nullable is False
    assert added["review_verdict"].enum == [_APPROVE, _MODIFY, _SKIPPED]


def test_the_card_carries_the_six_columns_the_decision_is_made_from_and_no_others():
    """A reviewer sees exactly what the signature reads, so what it reads is the card."""
    read = [
        column.name
        for entry in _review_stage().signature.reads
        for column in entry.columns
    ]

    assert read == [
        "record_id", "organisation", "public_commitment", "lobbying_issue",
        _JUDGED_COLUMN, "ai_justification",
    ]


def test_nothing_orders_the_queue_because_an_order_would_widen_the_card():
    """A sort key has to be read, and a read column is a column on the card."""
    assert _review_stage().queue.sort == []


def test_the_reviewer_is_told_to_read_both_texts_rather_than_the_label():
    instructions = _review_stage().queue.reviewer_instructions or ""

    assert "the model's label is a proposal" in instructions
    assert "which words in the two texts you decided from" in instructions
    # Real organisations: the reviewer's own prior is not evidence either.
    assert "what you already believe about this one is not evidence" in instructions


def test_the_review_step_queues_the_contradictions_and_halts_the_run(tmp_path):
    """Stand-in labels, real stage: what halts a run is the filter, not the model."""
    judged = _stand_in_for_the_model(_matched())
    contradictions = int((judged[_JUDGED_COLUMN] == _CONTRADICTS).sum())
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project="tutorial_queue_smoke", run_id="r1"),
        stage_cache=StageCacheEntry.read_write(),
    )

    with pytest.raises(HaltForReview) as halted:
        HANDLERS[StageType.human_review_queue].execute(
            place_stage(_review_stage()), as_inputs({_TARGET_STAGE: judged}), ctx)

    assert contradictions > 0
    assert halted.value.pending_count == contradictions


# ── the published report ─────────────────────────────────────────────────────


def _publish_a_report(tmp_path, df: pd.DataFrame) -> str:
    stage = _stage(_load_fixture(), "publish_report")
    # A project-scoped context, because the step declares `citation_provider` — the run's
    # (project, run_id) is what a row-trace URL is built from.
    ctx = RunContext.for_workflow_test_run(tmp_path, "tutorial", "R-1")
    out = HANDLERS[StageType(stage.type)].execute(
        place_stage(stage), as_inputs({_REVIEW_STAGE: df}), ctx)
    assert out is not None
    return Path(rows_of(out).iloc[0]["report_path"]).read_text(encoding="utf-8")


_SAID = "Publicly committed to supporting a national clean electricity standard."
_ASKED = "Opposed the national clean electricity standard in its comment letter."
_SAID_URL = "https://example.org/pledge"
_ASKED_URL = "https://example.org/comment-letter"


def _three_records() -> pd.DataFrame:
    """One contradiction a reviewer kept, one match and one record with no commitment."""
    return pd.DataFrame(
        [
            {
                "record_id": "R-1", "organisation": "Promise Breakers Mutual",
                "period": "2023-2024",
                "lobbying_issue": _ASKED, "lobbying_source_url": _ASKED_URL,
                "public_commitment": _SAID, "commitment_source_url": _SAID_URL,
                "ai_judgment": _CONTRADICTS,
                "reviewed_judgment": _CONTRADICTS, "review_verdict": _APPROVE,
                "reviewer": _REVIEWER, "reviewed_at": _REVIEWED_AT,
                "review_notes": "Both texts name the same standard, on opposite sides.",
            },
            {
                "record_id": "R-2", "organisation": "Consistent Cooperative",
                "period": "2024",
                "lobbying_issue": "Asked for faster offshore wind lease sales.",
                "lobbying_source_url": "https://example.org/testimony",
                "public_commitment": "Publicly committed to building out offshore wind.",
                "commitment_source_url": "https://example.org/investor-letter",
                "ai_judgment": "Matches",
                "reviewed_judgment": "Matches", "review_verdict": _SKIPPED,
                "reviewer": None, "reviewed_at": None, "review_notes": None,
            },
            {
                "record_id": "R-3", "organisation": "Silent Holdings", "period": "2025",
                "lobbying_issue": "Asked for shorter permitting timelines.",
                "lobbying_source_url": "https://example.org/letter",
                "public_commitment": None, "commitment_source_url": None,
                "ai_judgment": _NO_COMMITMENT,
                "reviewed_judgment": _NO_COMMITMENT, "review_verdict": _SKIPPED,
                "reviewer": None, "reviewed_at": None, "review_notes": None,
            },
        ]
    )


def test_the_report_links_every_text_to_the_page_it_was_read_on(tmp_path):
    """The link is the point: a reader who does not believe a row can open both sides."""
    page = _publish_a_report(tmp_path, _three_records())

    assert _SAID in page and _ASKED in page
    assert f'href="{_SAID_URL}"' in page and f'href="{_ASKED_URL}"' in page
    for _, row in _three_records().iterrows():
        assert f'href="{row["lobbying_source_url"]}"' in page


def test_a_published_contradiction_carries_who_confirmed_it_and_when(tmp_path):
    """The whole point of the review step: nobody publishes a machine's judgment unsigned."""
    page = _publish_a_report(tmp_path, _three_records().iloc[[0]])

    assert "Confirmed by" in page
    assert _REVIEWER in page and _REVIEWED_AT in page
    assert "Both texts name the same standard, on opposite sides." in page


def test_the_report_prints_the_label_the_reviewer_left_not_the_models(tmp_path):
    """A record the reviewer downgraded is no longer a contradiction on the page."""
    downgraded = _three_records().iloc[[0]].assign(
        reviewed_judgment="Unclear", review_verdict=_MODIFY,
        review_notes="The ask is about siting, which the pledge never mentions.",
    )

    page = _publish_a_report(tmp_path, downgraded)

    assert "1 advocacy records; 0 carry an ask" in page
    assert "Unclear" in page
    # What the model had said is still on the page, as what the reviewer changed it from.
    assert f"Changed from {_CONTRADICTS} by" in page
    assert _REVIEWER in page


def test_a_record_no_person_was_asked_about_carries_no_reviewer(tmp_path):
    page = _publish_a_report(tmp_path, _three_records().iloc[[1]])

    assert "Matches" in page
    assert "Confirmed by" not in page and "Changed from" not in page


def test_the_report_says_when_no_commitment_was_on_record(tmp_path):
    """The non-match is on the page, not only in lineage."""
    page = _publish_a_report(tmp_path, _three_records().iloc[[2]])

    assert "No public commitment on record" in page
    assert "1 have no public commitment on record" in page


def test_the_report_scores_nothing_and_recommends_nothing(tmp_path):
    page = _publish_a_report(tmp_path, _three_records())

    assert "reading aid, not a verdict" in page
    for verdict_word in ("score", "rank", "recommend", "priority"):
        assert verdict_word not in page.lower(), verdict_word


def test_the_report_says_the_judgement_is_a_persons_and_not_the_tools(tmp_path):
    """A page that reads as an accusation is one the tool is making. It is not."""
    page = _publish_a_report(tmp_path, _three_records())

    assert "This page makes no accusation of its own" in page
    assert "read off a published page" in page
    assert "What is printed is the person's answer" in page


def test_every_report_row_links_back_to_the_row_it_came_from(tmp_path):
    """A published claim nobody can trace is the thing this product exists against."""
    page = _publish_a_report(tmp_path, _three_records())

    for ordinal in range(len(_three_records())):
        assert f'href="/project/tutorial/runs/R-1/stage/{_REVIEW_STAGE}/row/{ordinal}' \
               '/trace/view">Lineage</a>' in page


def test_the_report_step_refuses_a_contradiction_it_cannot_print_both_sides_of(tmp_path):
    """Confirmed as contradicting yet carrying no commitment: there is no other side."""
    df = _three_records().iloc[[2]].assign(
        reviewed_judgment=_CONTRADICTS, review_verdict=_APPROVE,
        reviewer=_REVIEWER, reviewed_at=_REVIEWED_AT,
    )

    with pytest.raises(StepRefused, match="cannot say what it contradicts"):
        _publish_a_report(tmp_path, df)


def test_the_report_step_refuses_a_commitment_with_no_link(tmp_path):
    """Printing a claim with no source is the failure this whole workflow is built against."""
    df = _three_records().iloc[[1]].assign(commitment_source_url=None)

    with pytest.raises(StepRefused, match="no link to where it was said"):
        _publish_a_report(tmp_path, df)


def test_the_report_step_refuses_a_reviewed_judgment_nobody_reviewed(tmp_path):
    """A verdict with no reviewer on it is the model's, and is not published as a person's."""
    df = _three_records().iloc[[0]].assign(reviewer=None)

    with pytest.raises(StepRefused, match="cannot say who reviewed it"):
        _publish_a_report(tmp_path, df)
