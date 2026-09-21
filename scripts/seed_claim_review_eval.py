"""Seed and run the claim-review eval over one real, defective claim.

The case is the seeded AI-lobbying project's finished run, which read a 50-row
window: its headline total is an artifact of that cap, not a finding.

Usage:  python -m scripts.seed_claim_review_eval
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.core.paths import repo_root
from app.core.run_status import RunStatus
from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.evals.dataset_columns import deconflict_column_names, get_output_columns_from_stage
from app.evals.runner import run_eval
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.eval import ExpectedOutput, ScoringMetric
from app.models.records.claims import Claim, ClaimShape
from app.models.records.eval_config import EvalConfig
from app.models.records.eval_run import EvalRun
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.schema import Column, TableSchema
from app.models.stage import stage_to_spec_dict
from app.models.stages.input_data import FileFormat
from app.models.table import TableRef
from app.models.workflow import Workflow, parse_workflow
from app.models.workflow_stage import WorkflowStage
from app.services import drafts, run as run_service, versioning
from app.services.claim_shapes import load_claim_shapes, write_claim_shapes
from app.services.claims import load_claims_of_shape, submit_claim
from app.services.code_approval import approve_code_execution
from app.services.loader import save_stages
from app.services.project import create_project, find_projects_by_name, write_eval_config
from app.services.versioning import load_version_stages
from app.services.workspace import configure_projects_dir_from_env, resolve_project_dir

CASE_PROJECT_ID = "20260921T105528.171720"
CASE_RUN_ID = "20260921T105530.453113"
CASE_SLUG = "ai-spend"

SHAPE_LABEL = "Paid to outside firms to lobby on AI, in dollars"

CLAIM_TEXT = (
    "Clients paid outside lobbying firms $0 to lobby on artificial intelligence "
    "across the Q1 and Q2 2026 Senate LDA disclosures."
)

EXPECTED_DEFECT = (
    "The $0 total is an artifact of a row cap, not a finding about the data. The run "
    "read a 50-row window out of two full quarterly LDA exports, so the figure "
    "describes those 50 rows and says nothing about the disclosures as a whole."
)

CASE_ID = "ai-spend-row-window"
EVAL_PROJECT_NAME = "claim_review_eval"
REVIEW_MODEL = "sonnet"
JUDGE_MODEL = "claude-sonnet-4-6"

OVERRIDE_STAGE = "load_cases"
TARGET_STAGE = "judge_the_review"
VERDICT_COLUMN = "found"
EVAL_ID = "reviewers-name-the-defect"
EVAL_NAME = "Do the reviewers name the labelled defect?"
# `TableRef.path` is checkout-relative, so the file rides in the repository.
EVAL_DATASET_PATH = "scripts/data/claim_review_eval_cases.csv"

# What the case is labelled with: a reviewer SHOULD name the row window.
EXPECTED_VERDICT = True

EVAL_DESCRIPTION = (
    "The judging workflow reduces five reviewers' challenges to one boolean; this "
    "asks whether that boolean matches the labelled verdict for the case.")

EVAL_METHODOLOGY = """# Does a claim review catch a row-window artifact?

One case: a claim published off a run that read a 50-row window of a much larger
corpus. The five reviewers see the claim and the run's evidence, and a judge marks
whether any challenge they raised names the row window.

The judge marks found or missed against the labelled defect. It scores nothing.
"""

CODE_APPROVAL_REASON = (
    "The eval calls the claim-review seam in process; no declared stage type can "
    "start five reviewer agents over one claim."
)

REVIEW_CODE = """from app.services.claim_review_eval import review_the_claim_a_row_names


def transform(row):
    return {**row, **review_the_claim_a_row_names(row)}
"""

JUDGE_INSTRUCTIONS = """\
You are grading a claim-review system for the newsroom that runs it. That system \
sends a published claim to five reviewers, each of which raises challenges against \
the sentence. Your verdict decides whether this case counts as caught.

Each case carries a defect a person labelled by hand, and the challenges the five \
reviewers actually raised. You are shown nothing else: not who wrote the claim, not \
how the reviewers are prompted, not how any other case went. Judge the challenges in \
front of you against the one labelled defect.

An engineer reads your verdict beside the labelled defect and the raw challenge JSON, \
and is deciding whether to change the reviewer prompts. A false "found" hides a real \
blind spot, so mark found only where a challenge names the labelled defect itself — \
not a neighbouring worry about the same figure.

Do NOT produce a score, a percentage, or a grade of any kind. One case is marked \
found or missed; counting is someone else's job.

Return a JSON object with exactly these three fields:

1. "found": true where some challenge names the labelled defect, false otherwise.
2. "matching_challenge": the `text` of the challenge that names it, copied verbatim, \
or null where none does.
3. "why": one sentence saying what the matching challenge named, or — where you \
answered false — what the reviewers raised instead.

Worked example. Labelled defect: "The total is an artifact of a row cap: the run read \
50 rows of a much larger corpus." Challenges: [{"kind": "data", "text": "The run read \
50 of the corpus's rows, so this total is a floor, not the amount paid.", \
"severity": "major"}, {"kind": "wording", "text": "'Paid' should say 'reported as \
paid'.", "severity": "minor"}] ->
{"found": true, "matching_challenge": "The run read 50 of the corpus's rows, so this \
total is a floor, not the amount paid.", "why": "The first challenge names the 50-row \
window and says the total cannot stand for the corpus."}
"""

JUDGE_DATA_TEMPLATE = """\
Case: {case_id}

The defect a person labelled on this case:
{expected_defect}

The challenges the five reviewers raised, as JSON:
{challenges_json}
"""


def main() -> None:
    refuse_renamed_env_vars()
    configure_projects_dir_from_env()
    configure_default_stores()

    validate_the_case_run_is_held()
    shape = ensure_the_claim_shape()
    attach_the_shape_to_the_output(shape)
    claim = ensure_the_claim(shape)
    project_id = ensure_the_eval_project()
    cases_path = write_the_cases_file(project_id, claim)
    version_id = ensure_the_eval_version(project_id, cases_path)
    ensure_the_working_copy_holds_the_version(project_id, version_id)
    manifest = ensure_the_workflow_ran(project_id, version_id)
    config = save_the_eval_config(project_id, version_id, claim)
    eval_run = run_eval(project_id, config, version_id=version_id)

    print(f"claim:          {claim.id} ({claim.status})")
    print(f"eval project:   {project_id}")
    print(f"eval version:   {version_id}")
    print(f"workflow run:   {manifest.run_id} ({manifest.status})")
    describe_eval_run(eval_run)


def describe_eval_run(eval_run: EvalRun) -> None:
    print(f"eval run:       {eval_run.run_id} ({eval_run.status})")
    print(f"eval metrics:   {eval_run.metrics}")
    for note in eval_run.notes:
        print(f"eval note:      {note}")


def validate_the_case_run_is_held() -> None:
    published = [output for output in WorkflowOutput.list()
                 if output.citation.run_id == CASE_RUN_ID and output.slug == CASE_SLUG]
    if not published:
        raise SystemExit(
            f"this workspace holds no output '{CASE_SLUG}' of run '{CASE_RUN_ID}' in "
            f"project '{CASE_PROJECT_ID}' — the eval's one case is gone, and no other "
            f"run may stand in for it")


def ensure_the_claim_shape() -> ClaimShape:
    held = [shape for shape in load_claim_shapes(CASE_PROJECT_ID)
            if shape.label == SHAPE_LABEL]
    if held:
        return held[0]
    written = write_claim_shapes(CASE_PROJECT_ID, [ClaimShapeInput(
        label=SHAPE_LABEL,
        universe=DataUniverseRequirement.closed,
        importance=ClaimImportance.primary,
    )])
    return next(shape for shape in written if shape.label == SHAPE_LABEL)


def attach_the_shape_to_the_output(shape: ClaimShape) -> None:
    """The run minted its outputs before any shape existed, so the published row carries none."""
    output = read_the_case_output()
    if output.shape_id == shape.id:
        return
    if output.shape_id is not None:
        raise SystemExit(
            f"output '{CASE_SLUG}' already names shape '{output.shape_id}', not "
            f"'{shape.id}' — a claim would be filed under the wrong shape")
    output.shape_id = shape.id
    output.save()


def read_the_case_output() -> WorkflowOutput:
    for output in WorkflowOutput.list():
        if output.citation.run_id == CASE_RUN_ID and output.slug == CASE_SLUG:
            return output
    raise SystemExit(f"run '{CASE_RUN_ID}' published no output '{CASE_SLUG}'")


def ensure_the_claim(shape: ClaimShape) -> Claim:
    for claim in load_claims_of_shape(CASE_PROJECT_ID, shape.id):
        if claim.text == CLAIM_TEXT:
            return claim
    return submit_claim(CASE_PROJECT_ID, CASE_RUN_ID, CASE_SLUG, {}, CLAIM_TEXT)


def ensure_the_eval_project() -> str:
    held = find_projects_by_name(EVAL_PROJECT_NAME)
    project_id = held[0].id if held else create_project(
        EVAL_PROJECT_NAME, EVAL_METHODOLOGY, model=JUDGE_MODEL, source="seed script").id
    approve_code_execution(project_id, CODE_APPROVAL_REASON)
    return project_id


def write_the_cases_file(project_id: str, claim: Claim) -> Path:
    path = resolve_project_dir(project_id) / "data" / "cases.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "project_id", "claim_id", "model", "expected_defect"])
        writer.writerow([CASE_ID, CASE_PROJECT_ID, claim.id, REVIEW_MODEL, EXPECTED_DEFECT])
    return path


def ensure_the_eval_version(project_id: str, cases_path: Path) -> str:
    """Re-seeding must not mint a second version of stages the project already holds."""
    wanted = [stage_to_spec_dict(stage)
              for stage in parse_workflow(build_eval_stages(cases_path)).stages]
    for version in versioning.list_versions(project_id):
        if [stage_to_spec_dict(stage) for stage in version.stages] == wanted:
            return version.version_id
    return save_the_eval_version(project_id, cases_path)


def ensure_the_working_copy_holds_the_version(project_id: str, version_id: str) -> None:
    """The evals page reads the working copy, so a version alone shows every eval as broken."""
    save_stages(project_id, load_version_stages(project_id, version_id))


def ensure_the_workflow_ran(project_id: str, version_id: str) -> RunManifest:
    held = find_finished_run(project_id, version_id)
    if held is not None:
        return held
    run_service.execute(project_id, version_id=version_id)
    ran = find_finished_run(project_id, version_id)
    if ran is None:
        raise SystemExit(
            f"the run of version '{version_id}' recorded no manifest at status "
            f"'{RunStatus.OK}' — the five reviewers did not all answer")
    return ran


def find_finished_run(project_id: str, version_id: str) -> RunManifest | None:
    for entry in run_service.list_run_entries(project_id):
        held = entry.manifest
        if (held is not None and held.workflow_version == version_id
                and held.status == RunStatus.OK):
            return held
    return None


def save_the_eval_version(project_id: str, cases_path: Path) -> str:
    draft_id = drafts.create_draft(project_id).id
    drafts.write_draft_stages(project_id, draft_id, build_eval_stages(cases_path))
    saved = drafts.save_version(project_id, draft_id, message="one claim-review case")
    if not saved.ok or saved.version_id is None:
        raise SystemExit("the eval workflow was refused: " + "; ".join(saved.issues))
    return saved.version_id


def save_the_eval_config(project_id: str, version_id: str, claim: Claim) -> EvalConfig:
    workflow = Workflow(stages=load_version_stages(project_id, version_id))
    by_id = workflow.index_workflow_stages_by_id()
    override_columns = get_output_columns_from_stage(read_the_stage(by_id, OVERRIDE_STAGE))
    injected, expected = deconflict_column_names(
        override_columns, [read_the_verdict_column(read_the_stage(by_id, TARGET_STAGE))])
    write_the_eval_dataset(override_columns, injected, expected[0], claim)
    config = build_the_eval_config(project_id, injected + expected)
    write_eval_config(project_id, config)
    return config


def read_the_stage(by_id: dict[str, WorkflowStage], stage_id: str) -> WorkflowStage:
    if stage_id not in by_id:
        raise SystemExit(f"the saved version holds no stage '{stage_id}': {sorted(by_id)}")
    return by_id[stage_id]


def read_the_verdict_column(target: WorkflowStage) -> Column:
    for column in get_output_columns_from_stage(target):
        if column.name == VERDICT_COLUMN:
            return column
    raise SystemExit(
        f"stage '{target.id}' emits no '{VERDICT_COLUMN}' column for the scorer to read")


def write_the_eval_dataset(
    override_columns: list[Column], injected: list[Column], verdict: Column, claim: Claim,
) -> Path:
    values = read_the_labelled_values(override_columns, claim)
    row: dict[str, str | bool] = {
        dataset_column.name: values[stage_column.name]
        for dataset_column, stage_column in zip(injected, override_columns)}
    row[verdict.name] = EXPECTED_VERDICT
    path = repo_root() / EVAL_DATASET_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    return path


def read_the_labelled_values(
    override_columns: list[Column], claim: Claim,
) -> dict[str, str]:
    values = {"case_id": CASE_ID, "project_id": CASE_PROJECT_ID, "claim_id": claim.id,
              "model": REVIEW_MODEL, "expected_defect": EXPECTED_DEFECT}
    unlabelled = [column.name for column in override_columns if column.name not in values]
    if unlabelled:
        raise SystemExit(
            f"stage '{OVERRIDE_STAGE}' now produces {unlabelled}, which this script holds "
            f"no labelled value for — the eval dataset would be short a column")
    return values


def build_the_eval_config(project_id: str, columns: list[Column]) -> EvalConfig:
    return EvalConfig(
        eval_id=EVAL_ID, project=project_id, name=EVAL_NAME, description=EVAL_DESCRIPTION,
        override_stage=OVERRIDE_STAGE, target_stage=TARGET_STAGE,
        table=TableRef(path=EVAL_DATASET_PATH, format=FileFormat.csv,
                       table_schema=TableSchema(columns=columns)),
        expected_outputs=[ExpectedOutput(output_column=VERDICT_COLUMN,
                                         metric=ScoringMetric.exact)])


def build_eval_stages(cases_path: Path) -> list[dict[str, Any]]:
    return [_load_cases(cases_path), _review_the_claim(), _judge_the_review()]


def _column(name: str, type_: str, description: str = "") -> dict[str, Any]:
    return {"name": name, "type": type_, "nullable": True, "description": description}


_CASE_COLUMNS = [
    _column("case_id", "str", "Names this case."),
    _column("project_id", "str", "The project holding the claim under review."),
    _column("claim_id", "str", "The claim the reviewers are sent."),
    _column("model", "str", "Which model the five reviewers answer with."),
    _column("expected_defect", "str", "The defect a person labelled on this case."),
]


def _load_cases(cases_path: Path) -> dict[str, Any]:
    return {
        "id": "load_cases", "type": "input_data", "cache": True,
        "description": "Reads the labelled claim-review cases, one row each.",
        "connector": {"kind": "file",
                      "params": {"paths": [str(cases_path)], "format": "csv"}},
        "signature": {"form": "replaces", "produces": _CASE_COLUMNS},
    }


def _review_the_claim() -> dict[str, Any]:
    return {
        "id": "review_the_claim", "type": "python_row_function",
        # A cached row would replay a stored review instead of repeating the case.
        "cache": False,
        "description": "Sends the case's claim to the five reviewers and keeps their challenges.",
        "inputs": [{"id": "load_cases"}],
        "function": {
            "kind": "inline", "code": REVIEW_CODE,
            "summary": "Reviews the claim the row names, and returns the challenges raised.",
            "corner_cases": [{"case": "the row names no claim",
                              "expected": "the stage fails, naming the missing column"}],
        },
        "signature": {
            "form": "extends",
            "reads": [{"input": "load_cases", "columns": [
                _column("project_id", "str"), _column("claim_id", "str"),
                _column("model", "str")]}],
            "adds": [
                _column("challenge_count", "int", "How many challenges were raised."),
                _column("challenges_json", "str", "The challenges raised, as a JSON array."),
                _column("review_session_id", "str", "The agent session the review ran under."),
            ],
            "rewrites": [],
        },
    }


def _judge_the_review() -> dict[str, Any]:
    return {
        "id": "judge_the_review", "type": "llm_transform", "cache": True,
        "description": "Marks whether the reviewers named the defect this case was labelled with.",
        "inputs": [{"id": "review_the_claim"}],
        "compiler_notes": ["Grain is 1:1 — one verdict per labelled case."],
        "signature": {
            "form": "extends",
            "reads": [{"input": "review_the_claim", "columns": [
                _column("case_id", "str"), _column("expected_defect", "str"),
                _column("challenges_json", "str")]}],
            "adds": [
                _column("found", "bool", "True where a challenge names the labelled defect."),
                _column("matching_challenge", "str", "The challenge that names it, verbatim."),
                _column("why", "str", "One sentence on what the challenge named."),
            ],
            "rewrites": [],
        },
        "llm": {
            "prompt_instructions": JUDGE_INSTRUCTIONS,
            "prompt_data_template": JUDGE_DATA_TEMPLATE,
            "model": JUDGE_MODEL, "temperature": 0.0, "response_format": "json",
            "batch_size": 1, "thinking": "adaptive",
        },
        "workflow_outputs": [
            {"slug": "defect-found", "label": "Did the reviewers name the labelled defect",
             "primary": True, "kind": "figure", "column": "found"},
            {"slug": "challenges-raised", "label": "Challenges the five reviewers raised",
             "primary": False, "kind": "figure", "column": "challenge_count"},
            {"slug": "judge-reasoning", "label": "What the judge read the challenges as",
             "primary": False, "kind": "figure", "column": "why"},
        ],
    }


if __name__ == "__main__":
    main()
