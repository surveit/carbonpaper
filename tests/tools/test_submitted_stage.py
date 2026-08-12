"""SubmittedStage is what the add_stage tools bind: StageDraft plus the accommodation
for a client that pastes back a stage it read. The server-owned fields it carries are
dropped and named; nothing else about the submission is loosened, and the domain model
underneath keeps none of it."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import SERVER_OWNED_STAGE_FIELDS, StageDraft
from app.tools.submitted_stage import SubmittedStage

_ECHOED = {
    "id": "load", "type": "input_data", "description": "Load",
    "connector": {"kind": "file"},
    "tests": [], "source": {"section": "para 3"},
}


def test_a_submission_that_echoes_back_server_owned_fields_parses_and_records_them():
    submitted = SubmittedStage.model_validate(_ECHOED)

    assert submitted.dropped_server_owned_fields == ["tests", "source"]
    assert not set(SERVER_OWNED_STAGE_FIELDS) & set(submitted.to_stage_spec())


def test_the_domain_model_underneath_refuses_what_the_boundary_drops():
    with pytest.raises(ValidationError, match="tests"):
        StageDraft.model_validate(_ECHOED)


def test_schema_omits_the_fields_no_authoring_client_writes():
    properties = SubmittedStage.model_json_schema()["properties"]

    assert not set(SERVER_OWNED_STAGE_FIELDS) & set(properties)
    assert "dropped_server_owned_fields" not in properties
    assert "compiler_notes" in properties, "the authoring agent does set this one"


def test_an_unknown_field_is_still_refused():
    with pytest.raises(ValidationError, match="nonsense"):
        SubmittedStage.model_validate({
            "id": "load", "type": "input_data", "description": "Load",
            "connector": {"kind": "file"}, "nonsense": 1,
        })


def test_it_carries_no_cross_field_validator_of_its_own():
    """FastMCP binds before the handler runs, so a rule firing here surfaces as isError, not {ok, issues}."""
    validators = SubmittedStage.__pydantic_decorators__.model_validators
    assert {name for name, dec in validators.items() if dec.info.mode == "after"} == set()
