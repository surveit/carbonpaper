"""`llm.model` is required on LLMConfig, so nothing writes or loads a stage without one.

The rule is on the type, not the write path, so it also refuses a stage stored before it
existed — which is what alembic 0013 stamps, in the store and in every working copy.
"""
from __future__ import annotations

import json

import pytest

from app.services.errors import WorkflowLoadError
from app.services.loader import load_workflow_object
from app.services.stage_edit import add_stage_spec, edit_stage_spec

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
    (tmp_path / "compiled").mkdir()
    assert add_stage_spec(tmp_path, json.dumps(_source_spec())).ok
    return tmp_path


def test_adding_an_llm_stage_without_a_model_is_refused(project):
    result = add_stage_spec(project, json.dumps(_judge_spec()))
    assert not result.ok
    assert any("llm.model" in issue for issue in result.issues)


def test_a_model_off_the_menu_is_refused_and_the_refusal_lists_the_menu(project):
    result = add_stage_spec(project, json.dumps(_judge_spec(model="gpt-4")))
    assert not result.ok
    assert any("claude-haiku-4-5" in issue for issue in result.issues)


def test_adding_an_llm_stage_that_names_a_model_is_accepted(project):
    result = add_stage_spec(
        project, json.dumps(_judge_spec(model="claude-haiku-4-5")))
    assert result.ok, result.issues


def test_a_stage_stored_without_a_model_no_longer_loads(project):
    (project / "compiled" / "02_judge.json").write_text(
        json.dumps(_judge_spec()), encoding="utf-8")

    with pytest.raises(WorkflowLoadError) as caught:
        load_workflow_object(project)

    assert any("llm.model" in issue for issue in caught.value.issues)


def test_editing_a_stored_model_less_stage_is_refused_until_it_names_one(project):
    stored = project / "compiled" / "02_judge.json"
    stored.write_text(json.dumps(_judge_spec()), encoding="utf-8")
    # `edit_stage_spec` reads the whole workflow first, so the model-less neighbour
    # is what refuses the edit — naming a model on the way in is what lets it through.
    with pytest.raises(WorkflowLoadError):
        edit_stage_spec(project, "judge", json.dumps(_judge_spec(temperature=0.5)))

    stored.write_text(json.dumps(_judge_spec(model="claude-haiku-4-5")), encoding="utf-8")
    accepted = edit_stage_spec(
        project, "judge",
        json.dumps(_judge_spec(temperature=0.5, model="claude-opus-5")))
    assert accepted.ok, accepted.issues
