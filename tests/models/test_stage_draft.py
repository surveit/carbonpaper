"""StageDraft is the shape an authoring client sends; `Stage` stays the shape a
workflow stores. These hold the two together: anything the draft accepts must
rebuild as the same `Stage`, and the schema the draft ships must carry only what
a client can act on."""
from __future__ import annotations

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from app.models import StageDraft, parse_stage
from app.models.workflow import parse_workflow, validate_workflow_draft
from app.models.stage import AuthoredStageFields, SERVER_OWNED_STAGE_FIELDS, Stage, StageType, find_cache_ignored_reason
from app.seeds.seed import discover_workflow_files

# Every member of the `Stage` union, read off the union itself so a new stage
# type cannot be added without these structural checks covering it.
_STAGE_CLASSES = get_args(get_args(Stage)[0])

# SERVER_OWNED_STAGE_FIELDS are the four Stage fields StageDraft does not take:
# `tests` is written by generate_stage_tests, `eval`/`review` are human-authored,
# `source` is provenance the client does not set. A committed fixture dumps every
# Stage field, so a stage read out of one carries them; dropping those from a
# submission is the tool boundary's job (tests/tools/test_submitted_stage.py).


def _read_committed_example_stages() -> list[dict]:
    stages: list[dict] = []
    for path in discover_workflow_files():
        stages.extend(json.loads(path.read_text(encoding="utf-8"))["stages"])
    return stages


def test_every_committed_example_stage_round_trips_through_a_draft():
    stages = _read_committed_example_stages()
    assert len(stages) > 1, "no committed example stages to round-trip"

    for raw in stages:
        submitted = {k: v for k, v in raw.items() if k not in SERVER_OWNED_STAGE_FIELDS}
        rebuilt = parse_stage(StageDraft.model_validate(submitted).to_stage_spec())
        original = parse_stage(raw)
        expected = {
            k: v for k, v in original.model_dump(exclude_none=True).items()
            if k not in SERVER_OWNED_STAGE_FIELDS
        }
        assert rebuilt.model_dump(exclude_none=True) == expected, raw["id"]


def test_round_trip_covers_more_than_one_stage_type():
    types = {raw["type"] for raw in _read_committed_example_stages()}
    assert len(types) > 1, types


def test_an_input_round_trips_as_the_upstream_id_alone():
    draft = StageDraft.model_validate({
        "id": "flag_rows",
        "type": "python_row_function",
        "description": "Flag rows",
        "inputs": [{"id": "raw"}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "raw",
                    "columns": [{"name": "filing_id", "type": "str", "nullable": True}],
                },
            ],
        },
    })

    spec = draft.to_stage_spec()
    assert set(spec["inputs"][0]) == {"id"}

    rebuilt = parse_stage(spec)
    assert rebuilt.input_ids == ["raw"]


def test_a_stage_that_breaks_a_cross_field_rule_parses_as_a_draft_and_is_refused_by_stage():
    broken = {
        "id": "score_rows",
        "type": "llm_transform",
        "description": "Score rows",
        # the signature reads `text`, which the prompt never injects -> the
        # signature-vs-config rule fails
        "inputs": [{"id": "raw"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "raw",
                       "columns": [{"name": "text", "type": "str", "nullable": True}]}],
            "adds": [{"name": "score", "type": "float", "nullable": True}],
        },
        "llm": {"prompt_data_template": "score this"},
    }

    draft = StageDraft.model_validate(broken)  # must not raise

    with pytest.raises(ValidationError):
        parse_stage(draft.to_stage_spec())


def test_schema_omits_the_fields_no_authoring_client_writes():
    properties = StageDraft.model_json_schema()["properties"]
    assert not set(SERVER_OWNED_STAGE_FIELDS) & set(properties)
    assert "compiler_notes" in properties, "the authoring agent does set this one"


@pytest.mark.parametrize("stage_cls", _STAGE_CLASSES, ids=lambda c: c.__name__)
def test_every_stage_class_shares_the_drafts_field_list(stage_cls):
    assert issubclass(stage_cls, AuthoredStageFields) and issubclass(StageDraft, AuthoredStageFields)
    extra = set(stage_cls.model_fields) - set(StageDraft.model_fields)
    assert extra == set(SERVER_OWNED_STAGE_FIELDS), stage_cls.__name__


def test_the_draft_carries_no_cross_field_validator_of_its_own():
    """FastMCP binds before the handler runs, so a rule firing here surfaces as isError, not {ok, issues}."""
    validators = StageDraft.__pydantic_decorators__.model_validators
    assert {name for name, dec in validators.items() if dec.info.mode == "after"} == set()
    assert set(validators) == set(), "and no before-validator either: the draft is a plain shape"


def test_an_unknown_field_is_still_refused():
    with pytest.raises(ValidationError, match="nonsense"):
        StageDraft.model_validate({
            "id": "load", "type": "input_data", "description": "Load",
            "connector": {"kind": "file"}, "nonsense": 1,
        })


def test_stage_keeps_the_server_owned_fields_the_draft_never_declares():
    stage = parse_stage({
        "id": "load", "type": "input_data", "description": "Load",
        "connector": {"kind": "file"}, "source": {"section": "para 3"},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "filing_id", "type": "str", "nullable": True}],
        },
    })

    assert stage.source is not None
    assert "dropped_server_owned_fields" not in type(stage).model_fields
    assert "dropped_server_owned_fields" not in StageDraft.model_fields


# ── `cache` on a type that never consults one ────────────────────────────────
@pytest.mark.parametrize("stage_type", [
    "input_data", "enrich", "expand", "aggregate", "report", "union",
    "explode", "dedupe", "sort_rank", "python_frame_function",
])
def test_every_type_that_ignores_cache_says_why(stage_type):
    reason = find_cache_ignored_reason(StageType(stage_type))
    assert reason and not reason.endswith("."), "a clause the refusal reads into a sentence"


@pytest.mark.parametrize("stage_type", [
    "llm_transform", "python_row_function",
    "human_review_queue", "filter_rows", "starlark_row_function", "starlark_filter_rows",
])
def test_the_types_that_spend_per_row_honour_cache(stage_type):
    assert find_cache_ignored_reason(StageType(stage_type)) is None


_DEAD_CACHE_STAGE = {
    "id": "src", "type": "input_data", "description": "Source", "inputs": [],
    "cache": True, "connector": {"kind": "file", "params": {"format": "csv"}},
    "signature": {"form": "replaces", "reads": [], "produces": [
        {"name": "a", "type": "str", "nullable": False, "description": "A col."}]},
}


def test_authoring_refuses_a_cache_flag_the_type_ignores():
    issues = validate_workflow_draft([_DEAD_CACHE_STAGE])
    assert issues and "never consults one" in issues[0]
    assert "reads its input afresh" in issues[0], "the refusal quotes the type's own reason"


def test_a_workflow_already_carrying_one_still_loads():
    """Save is stricter than load: refusing here would strand every project that has one."""
    workflow = parse_workflow([_DEAD_CACHE_STAGE])
    assert workflow.stages[0].cache is True
