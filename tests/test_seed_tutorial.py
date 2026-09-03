from __future__ import annotations

import json
import re

from pathlib import Path

import pandas as pd

from app.evals.compatibility import validate_eval_compatibility
from app.evals.dataset_columns import get_injected_columns
from app.evals.store import load_eval_config
from app.core.files import list_project_files, save_upload
from app.models import Stage, Workflow
from app.models.review_guide import ReviewGuideDraft
from app.models.stages.human_review_queue import HumanReviewQueueStage
from app.services import project, run as run_service, uploads, versioning
from app.services.loader import load_workflow
from app.services.project import WorkflowFile, import_project
from app.tools.tutorial import (
    TutorialContext,
    read_seed_eval_config,
    seed_tutorial_project,
)
from conftest import pinned_stages

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "seeds" / "data" / "ai_lobbying_spend_2026.json"
)
_XLSX_Q1 = _FIXTURE_PATH.parent / "lda_data_Q1_2026.xlsx"
_XLSX_Q2 = _FIXTURE_PATH.parent / "lda_data_Q2_2026.xlsx"
_INPUT_FILES_BY_STAGE_ID = {"input_filings": [_XLSX_Q1, _XLSX_Q2]}
_GUIDE_PATH = _FIXTURE_PATH.parent / "review_guides" / _FIXTURE_PATH.name

_EXPECTED_STAGE_IDS = [
    "input_filings", "find_ai_mentions", "keep_ai_candidates", "judge_ai_substance",
    "keep_ai_lobbying", "read_reported_money", "flag_in_house_filings", "ai_filings",
    "select_external_filings", "review_ai_spend", "confirm_ai_spend",
    "ai_spend_by_client", "ai_spend_totals", "in_house_ai_filings",
    "in_house_ai_totals", "corpus_totals",
]

_EVAL_PATH = _FIXTURE_PATH.parent / "evals" / _FIXTURE_PATH.name
_EVAL_ID = "ai_substance_hard_cases"
_OVERRIDE_STAGE = "keep_ai_candidates"
_TARGET_STAGE = "judge_ai_substance"
_JUDGED_COLUMN = "is_about_ai"

_REVIEW_STAGE = "review_ai_spend"
_REVIEWED_COLUMN = "reviewed_is_about_ai"
_BASE_URL = "http://127.0.0.1:8788/"

# Small enough to run fast; proven to still reach judge_ai_substance.
_TOUR_LIMIT = 50

_JARGON = ("join", "merge", "key", "deduplicate", "schema", "grain", "enrich",
           "column", "null", "cast", "parse", "lineage", "upstream",
           "downstream", "row per", "row-level")
_COUNTED_NOUN = (
    r"records?|rows?|stages?|steps?|filings?|organisations?|clients?"
    r"|columns?|entries|entry|results?"
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


def _load_fixture() -> WorkflowFile:
    return WorkflowFile.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _stage(wf: WorkflowFile, stage_id: str) -> Stage:
    return next(stage for stage in wf.stages if stage.id == stage_id)


def _review_stage() -> HumanReviewQueueStage:
    stage = _stage(_load_fixture(), _REVIEW_STAGE)
    assert isinstance(stage, HumanReviewQueueStage)
    return stage


def _import_and_pin(tmp_path, label: str) -> tuple[str, str]:
    name = import_project(_load_fixture(), name=label)
    _workflow, version_id = pinned_stages(tmp_path / "examples" / name)
    return name, version_id


def _store_inputs(project_id: str) -> dict[str, list[str]]:
    ids: dict[str, list[str]] = {}
    for stage_id, paths in _INPUT_FILES_BY_STAGE_ID.items():
        ids[stage_id] = []
        for path in paths:
            with path.open("rb") as handle:
                ids[stage_id].append(save_upload(path.name, handle, project_id).id)
    return ids


def _run_capped(project_id: str) -> dict:
    bindings = {
        stage_id: uploads.resolve_files_binding(project_id, file_ids)
        for stage_id, file_ids in _store_inputs(project_id).items()
    }
    return run_service.execute(
        project_id, bindings=bindings, limits={"input_filings": _TOUR_LIMIT})


# ── the fixture itself ──────────────────────────────────────────────────────


def test_committed_fixture_imports_and_validates_cleanly(tmp_path):
    wf = _load_fixture()
    assert [stage.id for stage in wf.stages] == _EXPECTED_STAGE_IDS

    imported_name = import_project(wf, name="ai_lobbying_smoke")
    assert imported_name in project.list_projects()

    summary = project.read_workflow_summary(imported_name)
    assert summary.issues == []
    assert [stage.id for stage in summary.stages] == _EXPECTED_STAGE_IDS

    loaded_stages = load_workflow(imported_name)
    assert [stage.id for stage in loaded_stages] == _EXPECTED_STAGE_IDS
    assert len(versioning.list_versions(imported_name)) == 1

    assert _XLSX_Q1.is_file() and _XLSX_Q2.is_file()


def test_the_input_stage_carries_no_baked_in_path():
    stage = _stage(_load_fixture(), "input_filings")
    assert stage.connector.params.paths == []
    assert stage.connector.params.format == "xlsx"


def test_the_review_queue_reads_from_the_paid_filings():
    """The headline figure is the paid total, so that is what a person confirms."""
    queue_stage = _review_stage()
    assert [i.id for i in queue_stage.inputs] == ["select_external_filings"]


def test_the_queue_has_no_filter_every_paid_filing_is_reviewed():
    """Unlike a narrower queue, nothing here is published on the model's say-so alone."""
    assert _review_stage().queue.filter is None


def test_the_reviewers_answer_lands_beside_the_models_and_never_on_it():
    stage = _review_stage()
    added = {column.name: column for column in stage.signature.adds}

    assert stage.signature.rewrites == []
    assert _REVIEWED_COLUMN in added
    assert added[_REVIEWED_COLUMN].type == "bool"
    assert stage.queue.reviewed_columns == {_JUDGED_COLUMN: _REVIEWED_COLUMN}


def test_the_queue_sorts_by_reported_spend_descending():
    """The largest dollars are decided first — the ten biggest carry the most risk."""
    sort = _review_stage().queue.sort
    assert len(sort) == 1
    assert sort[0].column == "income_usd"
    assert sort[0].direction == "descending"


def test_the_card_carries_exactly_what_a_reviewer_needs_to_decide():
    stage = _review_stage()
    context = set(stage.queue.context_columns or [])
    read = {c.name for entry in stage.signature.reads for c in entry.columns}

    assert context <= read
    assert {"client", "income_usd"} <= context
    assert "filing_uuid" not in context, "an id is not something a reviewer judges"


def test_confirm_ai_spend_drops_a_filing_that_was_not_confirmed():
    stage = _stage(_load_fixture(), "confirm_ai_spend")
    assert [i.id for i in stage.inputs] == ["review_ai_spend"]
    assert f'row["{_REVIEWED_COLUMN}"]' in (stage.filter.code or "")


# ── the review guide ─────────────────────────────────────────────────────────


def test_the_committed_review_guide_accounts_for_every_stage():
    guide = ReviewGuideDraft.model_validate_json(_GUIDE_PATH.read_text(encoding="utf-8"))

    narrated = [stage_id for step in guide.steps for stage_id in step.stage_ids]
    assert narrated == _EXPECTED_STAGE_IDS
    assert guide.unnarrated == []
    for step in guide.steps:
        assert step.data_description and step.data_description.strip()
        assert len(step.prose) <= 255


def test_the_review_guide_speaks_no_jargon():
    """A journalist reads this rail. Every word in it has to be one they already use."""
    guide = ReviewGuideDraft.model_validate_json(_GUIDE_PATH.read_text(encoding="utf-8"))
    prose = " ".join(step.prose for step in guide.steps).lower()
    title = " ".join(step.title for step in guide.steps).lower()

    for word in _JARGON:
        assert word not in prose, word
        assert word not in title, word


def test_the_review_guide_states_no_count_and_names_no_place_on_the_screen():
    """The run measures the count and the layout moves: prose saying either goes stale silently."""
    guide = ReviewGuideDraft.model_validate_json(_GUIDE_PATH.read_text(encoding="utf-8"))

    offenders = [
        f"{step.title} / {field}: {hit!r}"
        for step in guide.steps
        for field, text in (
            ("title", step.title), ("prose", step.prose),
            ("data_description", step.data_description or ""),
        )
        for pattern in (_STATED_COUNT, _SCREEN_POSITION)
        for hit in pattern.findall(text)
    ]
    assert offenders == []


def test_the_committed_guide_validates_against_a_freshly_imported_version(tmp_path):
    """The authoritative check: the real validator, against a real imported version."""
    name, version_id = _import_and_pin(tmp_path, "ai_lobbying_smoke_guide")
    guide = project.read_review_guide(name, version_id)
    assert guide is None, "nothing writes the guide at import — the tour writes it separately"

    written = project.write_review_guide(
        name, version_id,
        ReviewGuideDraft.model_validate_json(_GUIDE_PATH.read_text(encoding="utf-8")))
    assert [s for step in written.steps for s in step.stage_ids] == _EXPECTED_STAGE_IDS


# ── the methodology document ─────────────────────────────────────────────────


def test_the_methodology_document_admits_the_data_is_real():
    wf = _load_fixture()
    assert "public" in wf.document.lower()
    assert "invented" not in wf.document.lower()


def test_every_stage_points_at_a_heading_the_document_still_carries():
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


# ── the eval ─────────────────────────────────────────────────────────────────


def _eval_dataset(project_id: str) -> pd.DataFrame:
    config = read_seed_eval_config(project_id)
    assert config.table is not None
    return pd.read_csv(Path(__file__).resolve().parents[1] / config.table.path)


def test_the_committed_eval_still_fits_the_workflow(tmp_path):
    name, version_id = _import_and_pin(tmp_path, "ai_lobbying_smoke_eval")
    wf_obj = Workflow(stages=versioning.load_version_stages(name, version_id))

    report = validate_eval_compatibility(read_seed_eval_config(name), wf_obj)

    assert report.ok, report.problems
    assert report.settings is not None
    assert report.settings.frontier == [_TARGET_STAGE]


def test_the_eval_dataset_supplies_every_column_the_override_stage_emits(tmp_path):
    name, version_id = _import_and_pin(tmp_path, "ai_lobbying_smoke_cols")
    wf_obj = Workflow(stages=versioning.load_version_stages(name, version_id))
    by_id = wf_obj.index_workflow_stages_by_id()

    injected = get_injected_columns(
        by_id[_OVERRIDE_STAGE], by_id[_TARGET_STAGE], [_JUDGED_COLUMN])

    assert set(_eval_dataset(name).columns) == {c.name for c in injected} | {_JUDGED_COLUMN}


def test_the_eval_labels_only_true_or_false(tmp_path):
    name, _version_id = _import_and_pin(tmp_path, "ai_lobbying_smoke_labels")
    dataset = _eval_dataset(name)
    assert set(dataset[_JUDGED_COLUMN]) <= {True, False}
    assert dataset[_JUDGED_COLUMN].notna().all()
    assert len(dataset) >= 6, "few enough to read, many enough to be a real check"


def test_the_committed_eval_names_no_project_of_its_own():
    assert "project" not in json.loads(_EVAL_PATH.read_text(encoding="utf-8"))


def test_the_committed_eval_dataset_reads_its_numeric_looking_columns_as_text(tmp_path):
    """A numeric-looking str column must survive read_csv without becoming a float."""
    from app.evals.dataset import read_table_ref

    name, _version_id = _import_and_pin(tmp_path, "ai_lobbying_smoke_eval_dtype")
    config = read_seed_eval_config(name)
    df = read_table_ref(config.table)

    assert df["income"].map(lambda v: pd.isna(v) or isinstance(v, str)).all()
    assert df["expenses"].map(lambda v: pd.isna(v) or isinstance(v, str)).all()
    assert df["year"].map(lambda v: isinstance(v, str)).all()


def test_the_tour_seeds_the_eval_beside_the_review_guide(projects_root):
    seeded = seed_tutorial_project(TutorialContext(base_url=_BASE_URL))

    stored = load_eval_config(seeded.project.id, _EVAL_ID)

    assert stored.project == seeded.project.id
    assert (stored.override_stage, stored.target_stage) == (_OVERRIDE_STAGE, _TARGET_STAGE)
    assert [check.output_column for check in stored.expected_outputs] == [_JUDGED_COLUMN]
    assert seeded.eval_id == _EVAL_ID


def test_a_second_tour_links_into_the_first_and_writes_nothing_over_it(projects_root):
    first = seed_tutorial_project(TutorialContext(base_url=_BASE_URL))
    guide = project.read_review_guide(first.project.id, first.version_id)
    renamed = load_eval_config(first.project.id, _EVAL_ID)
    renamed.name = "the name a reader gave it"
    renamed.save()

    second = seed_tutorial_project(TutorialContext(base_url=_BASE_URL))

    assert second.project.id == first.project.id
    assert project.read_review_guide(second.project.id, second.version_id).id == guide.id
    assert load_eval_config(second.project.id, _EVAL_ID).name == renamed.name
    assert second.input_files == first.input_files
    assert len(list_project_files(second.project.id)) == \
        len(_INPUT_FILES_BY_STAGE_ID["input_filings"])


# ── the seeded cache, and a real capped run through it ───────────────────────


def test_the_seeded_cache_answers_a_capped_run_offline(projects_root):
    """No model is available in this suite: getting through judge_ai_substance is the cache."""
    seeded = seed_tutorial_project(TutorialContext(base_url=_BASE_URL))

    result = _run_capped(seeded.project.id)

    by_stage = {r["stage_id"]: r for r in result["stage_records"]}
    assert by_stage["find_ai_mentions"]["output_row_count"] == _TOUR_LIMIT
    assert by_stage["judge_ai_substance"]["status"] == "ok"
    assert by_stage["judge_ai_substance"]["cached_rows"] == \
        by_stage["judge_ai_substance"]["output_row_count"] > 0


def test_the_seeded_cache_leaves_the_queue_open_for_the_reader(projects_root):
    """A cache built from an already-reviewed run would pre-answer the queue — it must not."""
    seeded = seed_tutorial_project(TutorialContext(base_url=_BASE_URL))

    result = _run_capped(seeded.project.id)

    assert result["status"] == "awaiting_review"
    by_stage = {r["stage_id"]: r for r in result["stage_records"]}
    assert by_stage[_REVIEW_STAGE]["status"] == "awaiting_review"
    assert by_stage["confirm_ai_spend"]["status"] == "pending"
