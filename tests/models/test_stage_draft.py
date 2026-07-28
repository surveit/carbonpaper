"""StageDraft is the shape an authoring client sends; `Stage` stays the shape a
workflow stores. These hold the two together: anything the draft accepts must
rebuild as the same `Stage`, and the schema the draft ships must carry only what
a client can act on."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models import Stage, StageDraft
from app.seeds.seed import discover_workflow_files

# The four Stage fields StageDraft does not take: `tests` is written by
# generate_stage_tests, `eval`/`review` are human-authored, `source` is
# provenance the client does not set. A committed fixture dumps every Stage
# field, so a stage read out of one carries them.
DROPPED_FIELDS = ("tests", "eval", "review", "source")


def _committed_example_stages() -> list[dict]:
    stages: list[dict] = []
    for path in discover_workflow_files():
        stages.extend(json.loads(path.read_text(encoding="utf-8"))["stages"])
    return stages


def test_every_committed_example_stage_round_trips_through_a_draft():
    stages = _committed_example_stages()
    assert len(stages) > 1, "no committed example stages to round-trip"

    for raw in stages:
        submitted = {k: v for k, v in raw.items() if k not in DROPPED_FIELDS}
        rebuilt = Stage.model_validate(StageDraft.model_validate(submitted).to_stage_spec())
        original = Stage.model_validate(raw)
        expected = {
            k: v for k, v in original.model_dump(exclude_none=True).items()
            if k not in DROPPED_FIELDS
        }
        assert rebuilt.model_dump(exclude_none=True) == expected, raw["id"]


def test_round_trip_covers_more_than_one_stage_type():
    """Guards the round-trip above against going vacuous: it only proves
    anything about the handle blocks the fixtures actually populate."""
    types = {raw["type"] for raw in _committed_example_stages()}
    assert len(types) > 1, types


def test_an_input_schema_round_trips_under_the_key_a_compiled_stage_spells():
    """InputRef's field is `table_schema` in python and `schema:` on the wire
    (pydantic reserves `schema` on BaseModel), so to_stage_spec must dump by
    alias — a spec keyed `table_schema` would be a shape no compiled stage has."""
    draft = StageDraft.model_validate({
        "id": "flag_rows",
        "type": "python_row_function",
        "name": "Flag rows",
        "inputs": [{"id": "raw", "schema": {
            "columns": [{"name": "filing_id", "type": "str"}],
            "primary_key": ["filing_id"],
        }}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })

    spec = draft.to_stage_spec()
    assert set(spec["inputs"][0]) == {"id", "schema"}

    rebuilt = Stage.model_validate(spec)
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
        "inputs": [{"id": "raw"}],  # no input schema -> not 1:1-checkable
        "llm": {"prompt_data_template": "score this"},
    }

    draft = StageDraft.model_validate(broken)  # must not raise

    with pytest.raises(ValidationError, match="primary_key"):
        Stage.model_validate(draft.to_stage_spec())


def test_schema_omits_the_fields_no_authoring_client_writes():
    properties = StageDraft.model_json_schema()["properties"]
    assert not set(DROPPED_FIELDS) & set(properties)
    assert "compiler_notes" in properties, "the authoring agent does set this one"
