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
from app.models import Column, Stage, StageType
from app.models.errors import StepRefused
from app.models.stages.signature import ExtendsSignature
from app.models.review_guide import ReviewGuideDraft
from app.models.stages.human_review_queue import HumanReviewQueueStage, ReviewVerdict
from app.runtime.context import RunContext, RunIdentity
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
from conftest import (
    as_inputs, make_run_context, pinned_stages, place_stage, require_awaiting_review, rows_of,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "seeds" / "data" / "tutorial_lobbying_triage.json"
)
_CSV_PATH = _FIXTURE_PATH.with_suffix(".csv")
_COMMITMENTS_PATH = _FIXTURE_PATH.parent / "tutorial_public_commitments.csv"
_CSV_BY_STAGE_ID = {"lobbying_filings": _CSV_PATH, "public_commitments": _COMMITMENTS_PATH}
_GUIDE_PATH = _FIXTURE_PATH.parent / "review_guides" / _FIXTURE_PATH.name
# Long enough to say what to check, short enough that the check is what is read.
_GUIDE_PROSE_CEILING = 210

_EXPECTED_STAGE_IDS = [
    "lobbying_filings",
    "public_commitments",
    "clean_filings",
    "filings_with_commitments",
    "judge_alignment",
    "review_contradictions",
    "publish_report",
]

# Counted off the committed CSVs.
_ROWS_IN_CSV = 24
_COMMITMENT_ROWS = 15
# Filings whose client has no row in the commitments file.
_UNMATCHED = 8
_BATCH_SIZE = 12
# The cap the tour's first run passes as limits {"lobbying_filings": N}.
_TOUR_LIMIT = 3
# Authored on clean_filings, the one stage of this workflow that may carry them.
_SEEDED_EXAMPLES = 5

_CONTRADICTS = "Contradicts"
_NO_COMMITMENT = "No commitment given"
_ALIGNMENT_VALUES = ["Contradicts", "Matches", "Unclear", _NO_COMMITMENT]

_REVIEW_STAGE = "review_contradictions"
_REVIEWED_COLUMN = "reviewed_judgment"
# The three verdicts the review runtime writes, as the fixture declares them.
_APPROVE, _MODIFY, _SKIPPED = "approve", "modify", "skipped"
# Stands in for a decision the run records: a name and the moment it was recorded.
_REVIEWER = "R. Vasquez"
_REVIEWED_AT = "2024-05-06T11:20:00"

_EVAL_PATH = _FIXTURE_PATH.parent / "evals" / _FIXTURE_PATH.name
_EVAL_ID = "alignment_hard_cases"
_OVERRIDE_STAGE = "filings_with_commitments"
_TARGET_STAGE = "judge_alignment"
_JUDGED_COLUMN = "ai_judgment"
# Counted off the committed eval dataset.
_EVAL_ROWS = 24
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


def _all_filings() -> pd.DataFrame:
    return pd.read_csv(_CSV_PATH)


def _checked(filings: pd.DataFrame | None = None) -> pd.DataFrame:
    return _execute(
        _stage(_load_fixture(), "clean_filings"),
        {"lobbying_filings": _all_filings() if filings is None else filings},
    )


def _joined() -> pd.DataFrame:
    return _execute(
        _stage(_load_fixture(), "filings_with_commitments"),
        {
            "clean_filings": _checked(),
            "public_commitments": pd.read_csv(_COMMITMENTS_PATH),
        },
    )


def _stand_in_for_the_model(df: pd.DataFrame) -> pd.DataFrame:
    # Fills what judge_alignment would add. Never calls a model.
    judged = df.copy()
    judged["ai_judgment"] = [
        "No commitment given" if pd.isna(said) else _ALIGNMENT_VALUES[i % 3]
        for i, said in enumerate(judged["public_commitment"])
    ]
    judged["ai_justification"] = [
        f"stand-in note for {filing_id}" for filing_id in judged["filing_id"]
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


def test_the_bundled_filings_csv_carries_the_row_count_the_tour_reports():
    df = pd.read_csv(_CSV_PATH)

    assert list(df.columns) == [
        "filing_id", "client", "registrant", "amount_usd", "filing_period", "lobbied_about",
    ]
    assert len(df) == _ROWS_IN_CSV


def test_the_bundled_commitments_csv_is_one_row_per_organisation():
    """What lets the join be an enrich: a repeated client would fail the run."""
    df = pd.read_csv(_COMMITMENTS_PATH)

    assert list(df.columns) == ["client", "public_commitment", "commitment_source"]
    assert len(df) == _COMMITMENT_ROWS
    assert df["client"].is_unique


def test_the_sample_shows_all_three_join_outcomes():
    """The tour needs a contradiction, a match and a non-match to point at."""
    joined = _joined()
    unmatched = joined[joined["public_commitment"].isna()]

    assert len(joined) == _ROWS_IN_CSV
    assert len(unmatched) == _UNMATCHED
    # Both sides of the say-versus-do frame are represented.
    asks = joined[joined["public_commitment"].notna()]["lobbied_about"]
    assert int(asks.str.startswith("Opposing").sum()) > 0
    assert int(asks.str.startswith("Supporting").sum()) > 0


def test_the_texts_are_short_enough_to_read_side_by_side():
    """The point of this sample: the contradiction is visible in one glance."""
    filings = pd.read_csv(_CSV_PATH)
    commitments = pd.read_csv(_COMMITMENTS_PATH)

    assert int(filings["lobbied_about"].str.len().max()) <= 120
    assert int(commitments["public_commitment"].str.len().max()) <= 120


def test_the_tours_capped_run_covers_a_contradiction_a_match_and_a_non_match():
    """Beat 2 caps lobbying_filings, so all three outcomes must be inside the cap."""
    wf = _load_fixture()
    capped = pd.read_csv(_CSV_PATH).head(_TOUR_LIMIT)

    joined = _execute(
        _stage(wf, "filings_with_commitments"),
        {"clean_filings": _checked(capped),
         "public_commitments": pd.read_csv(_COMMITMENTS_PATH)},
    )

    assert len(joined) == _TOUR_LIMIT
    assert int(joined["public_commitment"].isna().sum()) == 1
    matched = joined[joined["public_commitment"].notna()]["lobbied_about"]
    assert int(matched.str.startswith("Opposing").sum()) >= 1
    assert int(matched.str.startswith("Supporting").sum()) >= 1


# ── the join ─────────────────────────────────────────────────────────────────


def test_the_join_is_many_to_one_and_drops_no_filing():
    joined = _joined()
    filings = _checked()

    assert len(joined) == len(filings) == _ROWS_IN_CSV
    assert list(joined["filing_id"]) == list(filings["filing_id"])
    # Every subject column flows through untouched; the join only ever ADDS.
    for column in filings.columns:
        assert list(joined[column]) == list(filings[column])
    assert list(joined.columns)[-2:] == ["public_commitment", "commitment_source"]


def test_one_commitment_serves_several_filings_by_the_same_client():
    """The many-to-one case, which the runtime verifies rather than trusts."""
    kept = _all_filings()
    repeated = kept["client"].value_counts()

    assert int(repeated.max()) > 1, "no client files twice, so m:1 is never exercised"
    assert len(_joined()) == len(kept)


def test_an_unmatched_filing_survives_with_a_blank_commitment():
    """The non-match record: the filing's own fields are all still there."""
    joined = _joined()
    unmatched = joined[joined["public_commitment"].isna()]

    assert len(unmatched) == _UNMATCHED
    assert unmatched["commitment_source"].isna().all()
    for column in ("filing_id", "client", "amount_usd", "lobbied_about"):
        assert unmatched[column].notna().all()


def test_a_repeated_commitment_row_fails_the_run_rather_than_multiplying_filings():
    stage = _stage(_load_fixture(), "filings_with_commitments")
    commitments = pd.read_csv(_COMMITMENTS_PATH)
    doubled = pd.concat([commitments, commitments.head(1)], ignore_index=True)

    with pytest.raises(ValueError, match="public_commitments"):
        _execute(stage, {"clean_filings": _checked(), "public_commitments": doubled})


# ── the check ────────────────────────────────────────────────────────────────


def test_every_committed_filing_carries_a_spend_the_check_can_read():
    """If one could not be read the tour's own run would stop, which is not the demo."""
    checked = _checked()
    filings = _all_filings()

    assert set(checked["amount_usd"].map(type)) == {int}
    assert list(checked["amount_usd"]) == [
        int(text.replace("$", "").replace(",", "")) for text in filings["amount_usd"]
    ]


def test_clean_filings_is_grain_preserving_and_touches_nothing_else():
    checked = _checked()
    filings = _all_filings()

    assert len(checked) == len(filings) == _ROWS_IN_CSV
    assert list(checked.columns) == list(filings.columns)
    for column in filings.columns:
        if column != "amount_usd":
            assert list(checked[column]) == list(filings[column])


def test_the_seeded_examples_pass_before_anything_has_been_run():
    """Beat 3 sends the reader to this panel; a failing case there is the tour's first impression."""
    report = run_stage_tests(list(_load_fixture().stages))

    assert report.summary.stages_run == 1
    assert report.summary.tests_total == _SEEDED_EXAMPLES
    assert report.count_failing_by_stage() == {}
    # Every stage that could carry examples does.
    assert report.untested_stages == []


def test_every_seeded_example_is_one_of_the_bundled_filings():
    """An invented row reads as fabrication; each case names a filing in the committed CSV."""
    tests = _stage(_load_fixture(), "clean_filings").tests or []
    filing_ids = set(_all_filings()["filing_id"])

    assert len(tests) == _SEEDED_EXAMPLES
    for test in tests:
        row = test.inputs["lobbying_filings"][0]
        assert row["filing_id"] in filing_ids, test.name


@pytest.mark.parametrize("spend", ["n/a", "", "one hundred thousand", "$1,0 00.50"])
def test_a_spend_the_step_cannot_read_stops_the_run(spend):
    """The cardinal case: a figure nobody can read never becomes a blank downstream."""
    filings = _all_filings()
    filings.loc[0, "amount_usd"] = spend

    with pytest.raises(StepRefused, match=filings.loc[0, "filing_id"]):
        _checked(filings)


def test_a_filing_with_no_account_of_its_ask_stops_the_run():
    filings = _all_filings()
    filings.loc[0, "lobbied_about"] = "   "

    with pytest.raises(StepRefused, match="no ask to judge"):
        _checked(filings)


# ── the model step ───────────────────────────────────────────────────────────


def test_judge_alignment_reads_a_dozen_filings_per_model_call():
    judge = _stage(_load_fixture(), "judge_alignment")
    # Each call spawns a process, so the batch size is the tour's wall clock.

    assert judge.llm is not None
    assert judge.llm.batch_size == _BATCH_SIZE
    assert _ROWS_IN_CSV <= _BATCH_SIZE * 2
    for placeholder in ("{client}", "{public_commitment}", "{lobbied_about}"):
        assert placeholder in judge.llm.prompt_data_template


def test_the_model_is_shown_both_texts_and_told_to_use_nothing_else():
    """The organisations are invented, so any outside knowledge would be fabricated."""
    judge = _stage(_load_fixture(), "judge_alignment")
    assert judge.llm is not None
    instructions = judge.llm.prompt_instructions

    assert "Judge only the two texts in front of you" in instructions
    assert "never invent, complete or paraphrase a commitment" in instructions
    # The join leaves this blank for an unmatched filing, and pandas renders it `nan`.
    assert "No commitment given" in instructions and "`nan`" in instructions


def test_the_methodology_document_states_the_batch_size_the_stage_uses():
    wf = _load_fixture()
    # The document is the source of record a reviewer reads against the stage.

    # "up to": the last call of a run holds whatever is left over.
    assert "read up to twelve at a time in one model call" in wf.document
    assert "four at a time" not in wf.document
    # And what the saving costs, which the document owes a reader of the labels.
    assert "swayed by the ones it happens to travel with" in wf.document


def test_every_stage_points_at_a_heading_the_document_still_carries():
    """Nothing in the app reads `source.section`, so a renamed heading strands it in silence."""
    wf = _load_fixture()
    headings = {
        line.lstrip("#").strip() for line in wf.document.splitlines()
        if line.startswith("#")
    }

    pointed = {
        stage.source.section for stage in wf.stages
        if stage.source is not None and stage.source.section is not None
    }

    assert pointed <= headings, sorted(pointed - headings)


def test_the_methodology_document_admits_the_data_is_invented():
    wf = _load_fixture()

    assert "The sample data is INVENTED." in wf.document
    assert "no row describes a real filing, client, firm or commitment" in wf.document


def test_the_tours_capped_run_is_one_model_call():
    # What the tour's small run costs: beat 2 caps lobbying_filings.
    df = pd.read_csv(_CSV_PATH).head(_TOUR_LIMIT)

    assert len(df) <= _BATCH_SIZE


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


def test_the_review_guide_keeps_every_check_without_the_padding():
    """Each step is capped, and each capped step still names what a reviewer must check."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )

    over = [step.title for step in guide.steps if len(step.prose) > _GUIDE_PROSE_CEILING]
    assert not over, over
    prose = " ".join(step.prose for step in guide.steps)
    for check in (
        "Invented",
        "a repeat fails the run",
        "the absence IS the record",
        "Trust this step least",
        "stopped the run rather than travelling as a blank",
        # The guide ends where the run does: on the file it published, and every row
        # on it walks back — said without the word the jargon guard below refuses.
        "open it under Published",
        "linked back to the filing it came from",
    ):
        assert check in prose, check


_JARGON = ("join", "merge", "key", "deduplicate", "schema", "grain", "enrich",
           "column", "null", "cast", "parse", "lineage", "upstream",
           "downstream", "row per", "row-level")

# Words above that a journalist also writes in their own trade: a key finding, a newspaper
# column, a broadcast, a sparse record, a grain of salt, a merger. The document is long
# prose and would trip over the substring; a field caption is one line and does not.
_PROSE_HOMOGRAPHS = frozenset({"key", "column", "cast", "parse", "grain", "merge"})
_DOCUMENT_JARGON = tuple(word for word in _JARGON if word not in _PROSE_HOMOGRAPHS)


def test_the_review_guide_speaks_no_jargon():
    """A journalist reads this rail. Every word in it has to be one they already use."""
    guide = ReviewGuideDraft.model_validate_json(
        _GUIDE_PATH.read_text(encoding="utf-8")
    )
    prose = " ".join(step.prose for step in guide.steps).lower()

    for word in _JARGON:
        assert word not in prose, word


def test_no_stage_description_speaks_jargon():
    """The same reader meets these first: on the graph node and over the stage panel."""
    offenders = _find_jargon_in_stage_descriptions(_load_fixture())

    assert offenders == [], offenders


def test_no_column_description_speaks_jargon():
    """The Schema tab prints these beside the rows, to the reader the guide is written for."""
    offenders = _find_jargon_in_column_descriptions(_load_fixture())

    assert offenders == [], offenders


def test_no_transform_block_speaks_jargon():
    """The Transform tab leads on the summary and the cases, for a reviewer who reads no code."""
    offenders = _find_jargon_in_transform_prose(_load_fixture())

    assert offenders == [], offenders


def test_the_methodology_document_speaks_no_jargon():
    """The document is the account a reviewer reads the run against — same reader, same words."""
    document = " ".join(_load_fixture().document.lower().split())
    # Collapsed above because the document is hard-wrapped: `row per` sits in it as `row\nper`.

    offenders = [word for word in _DOCUMENT_JARGON if word in document]

    assert offenders == [], offenders


def _find_jargon_in_stage_descriptions(wf: WorkflowFile) -> list[str]:
    return [
        f"{stage.id}: {word!r} in {stage.description!r}"
        for stage in wf.stages
        for word in _JARGON
        if word in stage.description.lower()
    ]


def _find_jargon_in_column_descriptions(wf: WorkflowFile) -> list[str]:
    return [
        f"{stage.id}/{column.name}: {word!r} in {column.description!r}"
        for stage in wf.stages
        for column in _list_signature_columns(stage)
        for word in _JARGON
        if word in (column.description or "").lower()
    ]


def _find_jargon_in_transform_prose(wf: WorkflowFile) -> list[str]:
    return [
        f"{stage.id}/{label}: {word!r} in {text!r}"
        for stage in wf.stages
        for label, text in _list_authored_code_prose(stage)
        for word in _JARGON
        if word in text.lower()
    ]


def _list_authored_code_prose(stage: Stage) -> list[tuple[str, str]]:
    block = stage.find_authored_code_block()
    if block is None:
        return []
    prose = [("summary", block.summary or "")]
    for ordinal, corner_case in enumerate(block.corner_cases):
        prose.append((f"corner_case[{ordinal}].case", corner_case.case))
        prose.append((f"corner_case[{ordinal}].expected", corner_case.expected))
    return prose


def _list_signature_columns(stage: Stage) -> list[Column]:
    signature = stage.signature
    read = [column for entry in signature.reads for column in entry.columns]
    if isinstance(signature, ExtendsSignature):
        return [*read, *signature.adds, *signature.rewrites]
    return [*read, *signature.produces]


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


def test_the_eval_dataset_labels_only_with_verdicts_the_stage_may_emit():
    judge = _stage(_load_fixture(), _TARGET_STAGE)
    column = next(c for c in judge.signature.adds if c.name == _JUDGED_COLUMN)
    dataset = _eval_dataset()

    assert len(dataset) == _EVAL_ROWS
    # Every branch of the rubric is exercised, and none is invented.
    assert set(dataset[_JUDGED_COLUMN]) == set(column.enum or [])


def test_the_eval_labels_a_blank_commitment_and_nothing_else_as_unjudgeable():
    """Step 3 fixes that answer to the absence, so it may not appear beside a commitment."""
    dataset = _eval_dataset()

    blank = dataset["public_commitment"].isna()
    assert set(dataset.loc[blank, _JUDGED_COLUMN]) == {_NO_COMMITMENT}
    assert _NO_COMMITMENT not in set(dataset.loc[~blank, _JUDGED_COLUMN])


def test_scoring_the_eval_costs_two_model_calls():
    """What a reader re-running it pays: the target reads the dataset at its own batch size."""
    judge = _stage(_load_fixture(), _TARGET_STAGE)
    assert judge.llm is not None

    assert len(_eval_dataset()) <= judge.llm.batch_size * 2


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


def test_only_the_filings_the_model_called_contradictions_reach_a_reviewer():
    """The other three labels are published as nobody's finding, so nobody is asked."""
    queue = _review_stage().queue

    assert queue.filter == f"ai_judgment == '{_CONTRADICTS}'"
    assert queue.reviewed_columns == {_JUDGED_COLUMN: _REVIEWED_COLUMN}


def test_the_reviewers_label_lands_beside_the_models_and_never_on_it():
    stage = _review_stage()
    added = {column.name: column for column in stage.signature.adds}

    assert stage.signature.rewrites == []
    assert added[_REVIEWED_COLUMN].enum == _ALIGNMENT_VALUES
    # Non-nullable: every filing reaches the report carrying a label, reviewed or not.
    assert added[_REVIEWED_COLUMN].nullable is False
    assert added["review_verdict"].enum == [_APPROVE, _MODIFY, _SKIPPED]


def test_the_queue_is_worked_in_the_order_the_filings_arrived():
    """A sort key must also be read, and spend has no part in reading two texts."""
    assert _review_stage().queue.sort == []


def test_the_reviewer_is_told_what_they_are_here_to_decide_before_anything_else():
    instructions = _review_stage().queue.reviewer_instructions or ""

    assert instructions.startswith("Review whether this filing's ask contradicts")
    assert "public commitment" in instructions and "Contradicts" in instructions
    assert "Unclear" in instructions and "Matches" in instructions
    assert "Add a note" in instructions


def test_the_card_carries_the_decision_and_nothing_else():
    """A reviewer sees exactly what the signature reads, so the reads ARE the card."""
    read = [
        column.name
        for entry in _review_stage().signature.reads
        for column in entry.columns
    ]

    assert read == [
        "filing_id", "client", "lobbied_about", "public_commitment",
        _JUDGED_COLUMN, "ai_justification",
    ]


def test_the_tours_capped_run_leaves_one_filing_asking_the_opposite_of_a_promise():
    """Beat 2's capped run is meant to queue a card or two: real, and finishable."""
    joined = _execute(
        _stage(_load_fixture(), "filings_with_commitments"),
        {"clean_filings": _checked(pd.read_csv(_CSV_PATH).head(_TOUR_LIMIT)),
         "public_commitments": pd.read_csv(_COMMITMENTS_PATH)},
    )
    against_a_promise = joined[
        joined["public_commitment"].notna()
        & joined["lobbied_about"].str.startswith("Opposing")
    ]

    assert list(against_a_promise["filing_id"]) == ["TUT-2024-0001"]


def test_the_review_step_queues_the_contradictions_and_halts_the_run(tmp_path):
    """Stand-in labels, real stage: what halts a run is the filter, not the model."""
    judged = _stand_in_for_the_model(_joined())
    contradictions = int((judged[_JUDGED_COLUMN] == _CONTRADICTS).sum())
    ctx = make_run_context(
        run_dir=tmp_path,
        identity=RunIdentity(project="tutorial_queue_smoke", run_id="r1"),
        stage_cache=StageCacheEntry.read_write(),
    )

    output = HANDLERS[StageType.human_review_queue].execute(
        place_stage(_review_stage()), as_inputs({_TARGET_STAGE: judged}), ctx)

    assert contradictions > 0
    assert require_awaiting_review(output).pending_count == contradictions


# ── the published report ─────────────────────────────────────────────────────


def _publish_a_report(tmp_path, df: pd.DataFrame) -> str:
    stage = _stage(_bound_fixture(), "publish_report")
    # A project-scoped context, because the step declares `citation_provider` — the run's
    # (project, run_id) is what a row-trace URL is built from.
    ctx = RunContext.for_workflow_test_run(tmp_path, "tutorial", "R-1")
    out = HANDLERS[StageType(stage.type)].execute(
        place_stage(stage), as_inputs({_REVIEW_STAGE: df}), ctx)
    assert out is not None
    return Path(rows_of(out).iloc[0]["report_path"]).read_text(encoding="utf-8")


def _bound_fixture() -> WorkflowFile:
    """The fixture as committed — it needs no filling in to be a valid document."""
    return WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _three_filings() -> pd.DataFrame:
    """One contradiction a reviewer kept, one match and one filing with no commitment."""
    return pd.DataFrame(
        [
            {
                "filing_id": "F-1", "client": "Promise Breakers Mutual", "registrant": "Firm A",
                "amount_usd": 250000, "filing_period": "2024 Q1",
                "lobbied_about": "Opposing the national clean electricity standard.",
                "public_commitment": "Publicly committed to supporting a national clean electricity standard.",
                "commitment_source": "2023 climate pledge",
                "ai_judgment": _CONTRADICTS,
                "ai_justification": "Said it backed the standard; this filing opposes it.",
                "reviewed_judgment": _CONTRADICTS, "review_verdict": _APPROVE,
                "reviewer": _REVIEWER, "reviewed_at": _REVIEWED_AT,
                "review_notes": "Both texts name the same standard, on opposite sides.",
            },
            {
                "filing_id": "F-2", "client": "Consistent Cooperative", "registrant": "Firm B",
                "amount_usd": 60000, "filing_period": "2024 Q1",
                "lobbied_about": "Supporting faster offshore wind lease sales.",
                "public_commitment": "Publicly committed to building out offshore wind capacity.",
                "commitment_source": "2024 investor letter",
                "ai_judgment": "Matches",
                "ai_justification": "Said it would build offshore wind; this filing asks for that.",
                "reviewed_judgment": "Matches", "review_verdict": _SKIPPED,
                "reviewer": None, "reviewed_at": None, "review_notes": None,
            },
            {
                "filing_id": "F-3", "client": "Silent Holdings", "registrant": "Firm C",
                "amount_usd": 90000, "filing_period": "2024 Q1",
                "lobbied_about": "Seeking shorter permitting timelines.",
                "public_commitment": None, "commitment_source": None,
                "ai_judgment": _NO_COMMITMENT, "ai_justification": None,
                "reviewed_judgment": _NO_COMMITMENT, "review_verdict": _SKIPPED,
                "reviewer": None, "reviewed_at": None, "review_notes": None,
            },
        ]
    )


def test_the_report_shows_both_sides_of_a_contradiction(tmp_path):
    """A bare flag is not an argument: print what they said and what they lobbied for."""
    page = _publish_a_report(tmp_path, _three_filings())

    assert "Said" in page and "Lobbied for" in page
    assert "Publicly committed to supporting a national clean electricity standard." in page
    assert "Opposing the national clean electricity standard." in page
    assert "$250,000" in page
    # Both texts sit in the follow-up cell, in that order, so the cell stands alone.
    said_at = page.index("<span class=\"label\">Said</span>")
    assert page.index("<span class=\"label\">Lobbied for</span>") > said_at


def test_a_published_contradiction_carries_who_confirmed_it_and_when(tmp_path):
    """The whole point of the review step: nobody publishes a machine's judgment unsigned."""
    page = _publish_a_report(tmp_path, _three_filings().iloc[[0]])

    assert "Confirmed" in page
    assert _REVIEWER in page and _REVIEWED_AT in page
    assert "Both texts name the same standard, on opposite sides." in page


def test_the_report_prints_the_label_the_reviewer_left_not_the_models(tmp_path):
    """A filing the reviewer downgraded is no longer a contradiction on the page."""
    downgraded = _three_filings().iloc[[0]].assign(
        reviewed_judgment="Unclear", review_verdict=_MODIFY,
        review_notes="The filing asks about siting, which the pledge never mentions.",
    )

    page = _publish_a_report(tmp_path, downgraded)

    assert "1 filings; 0 ask government for the opposite" in page
    assert "Lobbied for" not in page
    assert "Unclear" in page
    # What the model had said is still on the page, as what the reviewer changed it from.
    assert f"Changed from {_CONTRADICTS}" in page
    assert _REVIEWER in page


def test_a_filing_no_person_was_asked_about_carries_no_reviewer(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings().iloc[[1]])

    assert "Matches" in page
    assert "Confirmed" not in page and "Changed from" not in page


def test_the_report_prints_no_contradiction_for_a_filing_that_matches(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings().iloc[[1]])

    assert "Lobbied for" not in page
    assert "Matches" in page
    assert "1 filings; 0 ask government for the opposite" in page


def test_the_report_says_when_no_commitment_was_on_record(tmp_path):
    """The join's non-match is on the page, not only in lineage."""
    page = _publish_a_report(tmp_path, _three_filings().iloc[[2]])

    assert "No public commitment on record" in page
    assert "1 have no public commitment on record" in page
    assert "Lobbied for" not in page


def test_the_report_scores_nothing_and_recommends_nothing(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings())

    assert "reading aid, not a verdict" in page
    for verdict_word in ("score", "rank", "recommend", "priority"):
        assert verdict_word not in page.lower(), verdict_word


def test_the_report_admits_the_data_is_invented(tmp_path):
    page = _publish_a_report(tmp_path, _three_filings())

    assert "Invented sample data bundled with this tutorial." in page
    assert "No row describes a real filing, client, firm or commitment." in page


def test_every_report_row_links_back_to_the_row_it_came_from(tmp_path):
    """A published claim nobody can trace is the thing this product exists against."""
    page = _publish_a_report(tmp_path, _three_filings())

    for ordinal in range(len(_three_filings())):
        assert f'href="/project/tutorial/runs/R-1/stage/{_REVIEW_STAGE}/row/{ordinal}' \
               '/trace/view">Lineage</a>' in page


def test_the_report_step_refuses_a_contradiction_it_cannot_print_both_sides_of(tmp_path):
    """Confirmed as contradicting yet carrying no commitment: there is no other side."""
    df = _three_filings().iloc[[2]].assign(
        reviewed_judgment=_CONTRADICTS, review_verdict=_APPROVE,
        reviewer=_REVIEWER, reviewed_at=_REVIEWED_AT,
    )

    with pytest.raises(StepRefused, match="cannot say what it contradicts"):
        _publish_a_report(tmp_path, df)


def test_the_report_step_refuses_a_reviewed_judgment_nobody_reviewed(tmp_path):
    """A verdict with no reviewer on it is the model's, and is not published as a person's."""
    df = _three_filings().iloc[[0]].assign(reviewer=None)

    with pytest.raises(StepRefused, match="cannot say who reviewed it"):
        _publish_a_report(tmp_path, df)


def test_the_seeded_cache_is_readable_by_the_project_it_lands_in(tmp_path):
    reference = seed_tutorial_project(TutorialContext(base_url="http://localhost:8000/"))
    entries = StageCacheEntry.read_only().find_project_entries(reference.project.id)
    live = {
        (stage.id, stage.compute_definition_fingerprint())
        for stage in load_workflow(reference.project.id)
    }
    unreachable = [e.stage_id for e in entries if (e.stage_id, e.stage_fingerprint) not in live]
    assert unreachable == [], (
        "the committed bundle no longer matches the committed fixture — rebuild it "
        "with `python -m scripts.build_tutorial_cache`"
    )


def test_the_seeded_cache_answers_every_filing_the_model_step_would_be_asked(tmp_path):
    reference = seed_tutorial_project(TutorialContext(base_url="http://localhost:8000/"))
    entries = StageCacheEntry.read_only().find_project_entries(reference.project.id)
    judged = [entry for entry in entries if entry.stage_id == "judge_alignment"]
    assert len(judged) == _ROWS_IN_CSV


def test_the_seeded_cache_leaves_the_queue_for_the_reader(tmp_path):
    reference = seed_tutorial_project(TutorialContext(base_url="http://localhost:8000/"))
    entries = StageCacheEntry.read_only().find_project_entries(reference.project.id)
    verdicts = {
        (entry.output_row or {}).get("review_verdict")
        for entry in entries if entry.stage_id == "review_contradictions"
    }
    assert verdicts == {ReviewVerdict.skipped.value}
