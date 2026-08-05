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
from app.models.stage import Stage, StageCommon
from app.seeds.seed import discover_workflow_files

# Every member of the `Stage` union, read off the union itself so a new stage
# type cannot be added without these structural checks covering it.
_STAGE_CLASSES = get_args(get_args(Stage)[0])

# The four Stage fields StageDraft does not take: `tests` is written by
# generate_stage_tests, `eval`/`review` are human-authored, `source` is
# provenance the client does not set. A committed fixture dumps every Stage
# field, so a stage read out of one carries them.
DROPPED_FIELDS = ("tests", "eval", "review", "source")


def _read_committed_example_stages() -> list[dict]:
    stages: list[dict] = []
    for path in discover_workflow_files():
        stages.extend(json.loads(path.read_text(encoding="utf-8"))["stages"])
    return stages


def test_every_committed_example_stage_round_trips_through_a_draft():
    stages = _read_committed_example_stages()
    assert len(stages) > 1, "no committed example stages to round-trip"

    for raw in stages:
        submitted = {k: v for k, v in raw.items() if k not in DROPPED_FIELDS}
        rebuilt = parse_stage(StageDraft.model_validate(submitted).to_stage_spec())
        original = parse_stage(raw)
        expected = {
            k: v for k, v in original.model_dump(exclude_none=True).items()
            if k not in DROPPED_FIELDS
        }
        assert rebuilt.model_dump(exclude_none=True) == expected, raw["id"]


def test_round_trip_covers_more_than_one_stage_type():
    """Guards the round-trip above against going vacuous: it only proves
    anything about the config block the fixtures actually populate."""
    types = {raw["type"] for raw in _read_committed_example_stages()}
    assert len(types) > 1, types


def test_an_input_schema_round_trips_under_the_key_a_compiled_stage_spells():
    """StageInput's field is `table_schema` in python and `schema:` on the wire
    (pydantic reserves `schema` on BaseModel), so to_stage_spec must dump by
    alias — a spec keyed `table_schema` would be a shape no compiled stage has."""
    draft = StageDraft.model_validate({
        "id": "flag_rows",
        "type": "python_row_function",
        "name": "Flag rows",
        "inputs": [{"id": "raw", "schema": {
            "columns": [{"name": "filing_id", "type": "str", "nullable": True}],
        }}],
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
    assert set(spec["inputs"][0]) == {"id", "schema"}

    rebuilt = parse_stage(spec)
    assert rebuilt.inputs[0].table_schema is not None
    assert [c.name for c in rebuilt.inputs[0].table_schema.columns] == ["filing_id"]


def test_a_stage_that_breaks_a_cross_field_rule_parses_as_a_draft_and_is_refused_by_stage():
    """The reason the draft carries no cross-field validators: the refusal has to
    come from `Stage` inside the handler, where it can be reported on the
    handler's own channel, not from parameter binding."""
    broken = {
        "id": "score_rows",
        "type": "llm_transform",
        "name": "Score rows",
        # the output drops the input's `text` -> the additive 1:1 rule fails
        "inputs": [{"id": "raw", "schema": {"columns": [{"name": "text", "type": "str", "nullable": True}]}}],
        "output_schema": {"columns": [
            {"name": "score", "type": "float", "nullable": True},
        ]},
        "llm": {"prompt_data_template": "score this"},
    }

    draft = StageDraft.model_validate(broken)  # must not raise

    with pytest.raises(ValidationError):
        parse_stage(draft.to_stage_spec())


def test_schema_omits_the_fields_no_authoring_client_writes():
    properties = StageDraft.model_json_schema()["properties"]
    assert not set(DROPPED_FIELDS) & set(properties)
    assert "compiler_notes" in properties, "the authoring agent does set this one"


@pytest.mark.parametrize("stage_cls", _STAGE_CLASSES, ids=lambda c: c.__name__)
def test_every_stage_class_shares_the_drafts_field_list(stage_cls):
    """The draft and the stored models held together by a common base, not by
    field lists kept in step by hand: a field added to StageCommon is on both,
    and a stage class adds only its own config blocks plus what the server
    itself writes."""
    assert issubclass(stage_cls, StageCommon) and issubclass(StageDraft, StageCommon)
    extra = set(stage_cls.model_fields) - set(StageDraft.model_fields)
    assert extra == set(DROPPED_FIELDS), stage_cls.__name__


def test_the_draft_carries_no_cross_field_validator_of_its_own():
    """Load-bearing: FastMCP binds the parameter before the handler runs, so a
    rule that fired here would surface as isError=true instead of the documented
    {ok, issues} refusal. The one model validator the draft does declare drops
    server-owned keys and cannot raise."""
    after_validators = StageDraft.__pydantic_decorators__.model_validators
    assert {name for name, dec in after_validators.items() if dec.info.mode == "after"} == set()


def test_a_draft_that_echoes_back_server_owned_fields_parses_and_records_them():
    """A client copying a stage out of read_stage sends fields only the server
    writes. They are dropped, not refused — and named, so the drop is not silent."""
    draft = StageDraft.model_validate({
        "id": "load", "type": "input_data", "name": "Load",
        "connector": {"kind": "file"},
        "tests": [], "source": {"section": "para 3"},
    })

    assert draft.dropped_server_owned_fields == ["tests", "source"]
    assert not set(DROPPED_FIELDS) & set(draft.to_stage_spec())


def test_an_unknown_field_is_still_refused():
    with pytest.raises(ValidationError, match="nonsense"):
        StageDraft.model_validate({
            "id": "load", "type": "input_data", "name": "Load",
            "connector": {"kind": "file"}, "nonsense": 1,
        })


def test_stage_keeps_the_server_owned_fields_the_draft_drops():
    """The drop is the draft's behavior alone — a stored stage has to hold the
    tests and provenance it drops, and does not declare the bookkeeping field
    that records the drop."""
    stage = parse_stage({
        "id": "load", "type": "input_data", "name": "Load",
        "connector": {"kind": "file"}, "source": {"section": "para 3"},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "filing_id", "type": "str", "nullable": True}],
        },
    })

    assert stage.source is not None
    assert "dropped_server_owned_fields" not in type(stage).model_fields
