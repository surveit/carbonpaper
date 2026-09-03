"""The authoring boundary refuses to WRITE an `llm_transform` that names no model.

Enforced on the write path, not on `LLMConfig` and not in `graph_issues`: `Workflow`
is built to RUN a stored version (app.services.run), so a rule in either place would
refuse to run every llm stage saved before the rule existed.
"""
from __future__ import annotations

import json

import pytest

from app.services.loader import load_workflow_object
from app.services.stage_edit import open_working_copy, add_stage_spec, edit_stage_spec
from stage_seed import add_stage

_COLUMNS = [{"name": "text", "type": "str", "nullable": True}]


def _judge_spec(**llm_extra):
    return {
        "id": "judge", "description": "Judge", "type": "llm_transform",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "src", "columns": _COLUMNS}],
            "adds": [{"name": "verdict", "type": "str", "nullable": True}],
        },
        "llm": {"prompt_data_template": "{text}", **llm_extra},
    }


def _source_spec():
    return {
        "id": "src", "description": "Source", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    }


@pytest.fixture
def project(tmp_path):
    name = tmp_path.name
    assert add_stage_spec(open_working_copy(name), json.dumps(_source_spec())).ok
    return name


def test_adding_an_llm_stage_without_a_model_is_refused(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_judge_spec()))
    assert not result.ok
    assert any("`llm.model` is required" in issue for issue in result.issues)


def test_the_refusal_lists_the_models_this_deployment_offers(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_judge_spec()))
    assert any("claude-haiku-4-5" in issue for issue in result.issues)


def test_adding_an_llm_stage_that_names_a_model_is_accepted(project):
    result = add_stage_spec(open_working_copy(project), json.dumps(_judge_spec(model="claude-haiku-4-5")))
    assert result.ok, result.issues


def test_a_stage_stored_without_a_model_still_loads(project):
    add_stage(project, _judge_spec())
    workflow = load_workflow_object(project)
    assert {stage.id for stage in workflow.stages} == {"src", "judge"}
    assert next(s for s in workflow.stages if s.id == "judge").llm.model is None


def test_editing_a_stored_model_less_stage_is_refused_until_it_names_one(project):
    add_stage(project, _judge_spec())
    refused = edit_stage_spec(open_working_copy(project), "judge", json.dumps(_judge_spec(temperature=0.5)))
    assert not refused.ok
    accepted = edit_stage_spec(open_working_copy(project), "judge",
        json.dumps(_judge_spec(temperature=0.5, model="claude-haiku-4-5")))
    assert accepted.ok, accepted.issues
