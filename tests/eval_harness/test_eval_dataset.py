from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from evals.harness.dataset import Case, Dataset, DatasetInvalid, read_dataset
from evals.harness.definition import ExpectedOutput, Judgement, Loaded
from fixed_output_eval import FIXED_OUTPUT_EVAL, AnswerInput, AnswerOutput, ExpectedAnswer


def test_a_dataset_parses_each_case_with_the_evals_own_models(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [
            {
                "case_id": "agrees",
                "input": {"answer": "yes"},
                "expected_outputs": [{"key": "answer", "equals": "yes"}],
            },
            {
                "case_id": "disagrees",
                "input": {"answer": "no"},
                "expected_outputs": [
                    {"key": "answer", "equals": "yes"},
                    {"key": "echo", "equals": "no"},
                ],
            },
        ],
    )

    dataset = read_dataset(path, FIXED_OUTPUT_EVAL)

    assert dataset == Dataset(
        eval=FIXED_OUTPUT_EVAL.name,
        cases=[
            Case(
                case_id="agrees",
                input=AnswerInput(answer="yes"),
                expected_outputs=[ExpectedAnswer(key="answer", equals="yes")],
            ),
            Case(
                case_id="disagrees",
                input=AnswerInput(answer="no"),
                expected_outputs=[
                    ExpectedAnswer(key="answer", equals="yes"),
                    ExpectedAnswer(key="echo", equals="no"),
                ],
            ),
        ],
    )


def test_every_invalid_case_is_reported_at_once_and_a_valid_case_is_not(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [
            {
                "case_id": "two_problems",
                "input": {"answer": 7},
                "expected_outputs": [
                    {"key": "answer", "equals": "7"},
                    {"key": "echo", "weight": 2},
                ],
            },
            {"case_id": "expects_nothing", "input": {"answer": "yes"}, "expected_outputs": []},
            build_case("well_formed", "answer"),
        ],
    )

    message = read_refusal(path)

    assert "case 'two_problems' input.answer" in message
    assert "case 'two_problems' expected_outputs[1].equals" in message
    assert "case 'two_problems' expected_outputs[1].weight" in message
    assert "case 'expects_nothing'" in message
    assert "'well_formed'" not in message


def test_only_repeated_case_ids_are_refused_before_any_case_is_read(tmp_path: Path) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [
            build_case("twice", "answer"),
            build_case("once", "answer"),
            {"case_id": "twice", "input": {"answer": 7}, "expected_outputs": []},
        ],
    )

    message = read_refusal(path)

    assert "case_id 'twice' is repeated" in message
    assert "'once'" not in message
    assert "input.answer" not in message


def test_an_expected_key_repeated_within_a_case_is_refused_but_not_one_shared_across_cases(
    tmp_path: Path,
) -> None:
    path = write_dataset(
        tmp_path,
        FIXED_OUTPUT_EVAL.name,
        [build_case("repeats_a_key", "answer", "answer"), build_case("shares_a_key", "answer", "echo")],
    )

    message = read_refusal(path)

    assert "case 'repeats_a_key' lists expected key 'answer' more than once" in message
    assert "'shares_a_key'" not in message


def test_a_case_expecting_nothing_is_refused(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, FIXED_OUTPUT_EVAL.name, [build_case("expects_nothing")])

    message = read_refusal(path)

    assert "case 'expects_nothing' has no expected outputs" in message


def test_a_dataset_written_for_another_eval_is_refused_before_any_case_is_read(
    tmp_path: Path,
) -> None:
    path = write_dataset(
        tmp_path,
        "another_eval",
        [{"case_id": "claim_case", "input": {"claim": "x"}, "expected_outputs": [{"key": "verdict"}]}],
    )

    message = read_refusal(path)

    assert f"dataset is for eval 'another_eval', not {FIXED_OUTPUT_EVAL.name!r}" in message
    assert "'claim_case'" not in message


def test_a_dataset_without_cases_is_refused(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, FIXED_OUTPUT_EVAL.name, [])

    assert read_refusal(path) == f"{path}: dataset has no cases"


def test_fields_a_dataset_does_not_define_are_refused(tmp_path: Path) -> None:
    path = write_json(
        tmp_path,
        {
            "eval": FIXED_OUTPUT_EVAL.name,
            "version": 2,
            "cases": [
                {
                    "case_id": "noted",
                    "input": {"answer": "yes"},
                    "expected_outputs": [{"key": "answer", "equals": "yes"}],
                    "note": "not a case field",
                }
            ],
        },
    )

    message = read_refusal(path)

    assert "dataset.version" in message
    assert "dataset.cases[0].note" in message


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (ExpectedOutput, {"key": "answer"}),
        (Judgement, {"key": "answer", "outcome": "matched", "note": "answer 'yes' equals 'yes'"}),
        (Loaded[AnswerOutput], {"output": {"answer": "yes"}, "cost_usd": 0.01}),
    ],
)
def test_the_contract_models_forbid_extra_fields(
    model: type[BaseModel], fields: dict[str, JsonValue]
) -> None:
    model.model_validate(fields)

    with pytest.raises(ValidationError) as refusal:
        model.model_validate({**fields, "unexpected": True})

    assert [error["type"] for error in refusal.value.errors()] == ["extra_forbidden"]


def write_dataset(tmp_path: Path, eval_name: str, cases: list[JsonValue]) -> Path:
    return write_json(tmp_path, {"eval": eval_name, "cases": cases})


def write_json(tmp_path: Path, document: JsonValue) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def build_case(case_id: str, *expected_keys: str) -> JsonValue:
    return {
        "case_id": case_id,
        "input": {"answer": "yes"},
        "expected_outputs": [{"key": key, "equals": "yes"} for key in expected_keys],
    }


def read_refusal(path: Path) -> str:
    with pytest.raises(DatasetInvalid) as refusal:
        read_dataset(path, FIXED_OUTPUT_EVAL)
    return str(refusal.value)
